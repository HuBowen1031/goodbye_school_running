import Foundation

// ══════════════════════════════════════════════════════════════════
//  SidecarClient.swift —— 启动侧车 + JSON Lines 收发
//
//  ⚠️ 解释器路径**绝不硬编码**（findings.md F7）
//     文档里曾写死 `/Users/yuanjun/miniconda3/envs/vrun/bin/python`，
//     而本机该路径**根本不存在**（环境实际在 `~/.conda/envs/vrun`），
//     写死会让侧车**启动即失败** —— 属于最难排查的一类问题。
//     唯一入口是 `scripts/resolve_python.sh`。
//
//  为什么用 stdio 而不是 socket（works.md §4.2）
//     少一个 socket 文件、少一层权限问题，且**生命周期天然绑定**：
//     GUI 退出 → 管道断开 → 侧车读到 EOF → 自动退出，不留孤儿进程。
// ══════════════════════════════════════════════════════════════════

/// 侧车连接的一侧崩溃时的回调。
protocol SidecarClientDelegate: AnyObject {
    func sidecar(_ c: SidecarClient, didReceive event: SidecarEvent)
    func sidecar(_ c: SidecarClient, didReceive result: EstimateResult)
    func sidecar(_ c: SidecarClient, didLog entry: LogEntry)
    func sidecar(_ c: SidecarClient, didChangeState state: SidecarClient.State)
}

@MainActor
final class SidecarClient: NSObject, ObservableObject {

    enum State: Equatable {
        case stopped
        case starting
        case running
        case failed(String)

        var isRunning: Bool { self == .running }
    }

    // MARK: - 状态

    private(set) var state: State = .stopped {
        didSet {
            guard state != oldValue else { return }
            delegate?.sidecar(self, didChangeState: state)
        }
    }

    weak var delegate: SidecarClientDelegate?

    private var process: Process?
    private var inPipe: Pipe?
    private var outPipe: Pipe?
    private var reader: FileHandle?
    private var buffer = Data()

    /// 工具根目录。由 MainView 注入 —— 不从 Bundle 里找，因为
    /// M5 明确「不追求完全自包含，可靠性优先」（works.md §6 M5）。
    private let repoRoot: URL

    // MARK: - 初始化

    init(repoRoot: URL) {
        self.repoRoot = repoRoot
        super.init()
    }

    // MARK: - 解释器解析

    /// 唯一的解释器路径来源：`scripts/resolve_python.sh`。
    ///
    /// 它按 `$VRUN_PYTHON` → `conda config --show envs_dirs` →
    /// `conda env list --json` → 约定位置 的顺序探测，
    /// 且**找不到就报错、绝不静默回落**（findings.md F7）。
    private func resolvePython() throws -> String {
        let script = repoRoot.appendingPathComponent("scripts/resolve_python.sh")
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            throw SidecarError(message: "找不到可执行的 \(script.path)\n请确认 VRun.app 与仓库的相对位置没变。")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = [script.path]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        try p.run()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else {
            throw SidecarError(message: "resolve_python.sh 没能解出解释器路径。")
        }
        let path = String(data: out.fileHandleForReading.readDataToEndOfFile(),
                          encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !path.isEmpty else { throw SidecarError(message: "解释器路径为空。") }
        return path
    }

    // MARK: - 生命周期

    func start() {
        guard state == .stopped || isFailed else { return }
        state = .starting

        do {
            let python = try resolvePython()
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            // -m vrun serve：GUI 唯一会调的子命令
            p.arguments = ["-m", "vrun", "serve"]
            p.currentDirectoryURL = repoRoot
            // 让 `import vrun` 找到 src/，无需 pip install
            var env = ProcessInfo.processInfo.environment
            env["PYTHONPATH"] = repoRoot.appendingPathComponent("src").path
            env["PYTHONUNBUFFERED"] = "1"      // 关掉缓冲，保证实时
            p.environment = env

            let toSidecar = Pipe(), fromSidecar = Pipe()
            p.standardInput = toSidecar
            p.standardOutput = fromSidecar
            // stderr 直通终端 —— 侧车的启动失败信息在这里，不能丢
            p.standardError = FileHandle.standardError

            log(.info, "启动侧车：\(python) -m vrun serve")
            try p.run()

            process = p
            inPipe = toSidecar
            outPipe = fromSidecar
            state = .running
            log(.success, "侧车已连接")

            readLoop(fromSidecar.fileHandleForReading)

            // 侧车意外退出时要让 UI 知道，而不是默默卡住
            p.terminationHandler = { [weak self] proc in
                Task { @MainActor [weak self] in
                    guard let self, self.state.isRunning else { return }
                    let code = proc.terminationStatus
                    self.log(.error, "侧车意外退出（码 \(code)）")
                    self.state = .failed("侧车退出，码 \(code)")
                }
            }
        } catch {
            let msg = (error as? SidecarError)?.text ?? error.localizedDescription
            log(.error, "侧车启动失败：\(msg)")
            state = .failed(msg)
        }
    }

    private var isFailed: Bool { if case .failed = state { return true }; return false }

    func stop() {
        // 关闭 stdin → 侧车读到 EOF → 自行退出（works.md §4.2）
        try? inPipe?.fileHandleForWriting.close()
        if let p = process, p.isRunning {
            p.terminate()
            // 给 1 秒优雅退出，之后再强杀
            let deadline = Date().addingTimeInterval(1.0)
            while p.isRunning && Date() < deadline {
                usleep(50_000)
            }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
        process = nil; inPipe = nil; outPipe = nil; reader = nil
        state = .stopped
        log(.info, "侧车已关闭")
    }

    // MARK: - 收发

    func send(_ cmd: Command) {
        guard let fh = inPipe?.fileHandleForWriting else {
            log(.error, "发送失败：侧车未连接")
            return
        }
        guard let data = try? JSONEncoder().encode(cmd) else {
            log(.error, "命令编码失败：\(cmd.command)")
            return
        }
        do {
            try fh.write(contentsOf: data)
            try fh.write(contentsOf: Data("\n".utf8))
            log(.debug, "→ \(cmd.command)")
        } catch {
            log(.error, "写入失败：\(error.localizedDescription)")
        }
    }

    /// ★ 每行必须及时 flush（侧车 ipc.py 的坑①）——
    /// 这里反过来：读到一行**立刻**处理，不攒。
    private func readLoop(_ fh: FileHandle) {
        reader = fh
        reader?.readabilityHandler = { [weak self] h in
            let chunk = h.availableData
            guard !chunk.isEmpty else {
                Task { @MainActor [weak self] in
                    self?.reader?.readabilityHandler = nil
                    self?.reader = nil
                }
                return
            }
            Task { @MainActor [weak self] in self?.consume(chunk) }
        }
    }

    private func consume(_ chunk: Data) {
        buffer.append(chunk)
        // JSON Lines：一行一条。只取最后一个换行之前的内容，避免半包。
        while let idx = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<idx)
            buffer.removeSubrange(...idx)
            guard !line.isEmpty else { continue }
            handle(line: line)
        }
    }

    private func handle(line: Data) {
        // 先按完整事件解，再尝试 estimate.result（字段不同，解码会失败）
        if let r = try? JSONDecoder().decode(EstimateResult.self, from: line),
           let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
           obj["event"] as? String == "estimate.result" {
            delegate?.sidecar(self, didReceive: r)
            return
        }
        guard let ev = try? JSONDecoder().decode(SidecarEvent.self, from: line) else {
            log(.warn, "无法解析的一行：\(String(data: line, encoding: .utf8) ?? "<binary>")")
            return
        }
        log(.debug, "← \(ev.event)")
        if ev.event == "error" {
            let hint = (ev.hint?.isEmpty == false) ? "　提示：\(ev.hint!)" : ""
            log(.error, "\(ev.message ?? "")\(hint)")
        }
        delegate?.sidecar(self, didReceive: ev)
    }

    private func log(_ level: LogLevel, _ text: String) {
        delegate?.sidecar(self, didLog: .now(level, text))
    }
}

/// 侧车启动期的可读错误。`message` 只是给日志看的标签。
struct SidecarError: Error {
    let text: String
    init(message: String) { self.text = message }
}
