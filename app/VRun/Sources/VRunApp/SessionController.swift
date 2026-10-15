import SwiftUI

// ══════════════════════════════════════════════════════════════════
//  SessionController.swift —— 会话状态机
//
//  状态：空闲 → 预置 → 运行 → 暂停 → 停止 → 已清理
//
//  ★ 为什么状态必须集中在一处（file-manifest §5.1）
//     若散落在各 View 的 @State 里，「清理逻辑被漏调」的概率极高 ——
//     而漏调一次的后果是：博文的 iPhone 定位**一直卡在校园的虚拟点上**。
//     集中之后，「什么时候必须清理」只有一个答案。
//
//  ★ 运行中锁滑块（works.md §5.7）
//     跑到一半改速度会让弧长积分断裂，轨迹出现不连续跳变。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class SessionController: ObservableObject {

    enum Phase: String {
        case idle       = "空闲"
        case prepared   = "已预置"
        case running    = "运行中"
        case paused     = "已暂停"
        case stopped    = "已停止"
        case cleaned    = "已清理"
        case failed     = "异常"

        var isActive: Bool { self == .running || self == .prepared }
    }

    // MARK: - 面板参数（滑块）

    /// ★ 速度的**唯一**真值来源。
    ///
    /// ⚠️⚠️ 绝不能写成 `@Published var speedKmh { didSet { speedKmh = … } }`
    ///    —— 那样会**无限递归直到爆栈**（2026-10-13 实测崩溃，
    ///    诊断报告里是 `Thread stack size exceeded due to excessive recursion`，
    ///    递归深度 87059 帧，全部落在 `speedKmh.setter` ↔ `speedKmh.didset` 之间，
    ///    栈底是 `SpeedPanel.slider` 的 Binding setter）。
    ///
    ///    原因：`@Published` 把 setter 包成了 `Published.withMutation`，
    ///    Swift「在同一属性的观察器里赋值不再触发自己」这条保护因此失效，
    ///    赋值会重新进回 didSet，无限循环。
    ///
    ///    正解：**在写入侧夹紧一次**，存进一个不做任何观察的私有存储。
    @Published private var speedStorage: Double = 10.0

    /// 目标速度 km/h。定义域永远是 R3/R4 判定线 [6, 17]。
    var speedKmh: Double {
        get { speedStorage }
        set { speedStorage = min(max(newValue.isFinite ? newValue : Rules.vMin,
                                     Rules.speedRange.lowerBound),
                                 Rules.speedRange.upperBound) }
    }

    @Published var targetDistanceM: Double = 3000
    @Published var sampleRate: Double = 1.0          // ★ 默认 1 Hz，不是越高越好（§3.5）
    @Published var noise: NoiseLevel = .realistic
    /// 滑块映射：默认配速线性（§5.2）
    @Published var paceLinear: Bool = true

    // MARK: - 预览

    /// 松手后由侧车回填的精确值；为 nil 表示还没问过侧车。
    @Published private(set) var exact: EstimateResult?
    /// 本地近似与侧车精确值的差（秒）。> 1 s 要高亮（§5.6）。
    @Published private(set) var driftSeconds: Double = 0

    // MARK: - 会话

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var deviceConnected = false
    /// 设备名（`iPhone 18,1` 之类）。未连接时为 nil。
    @Published private(set) var deviceName: String?
    /// 上一次 ``device.status`` 的错误文本，刷新失败时显示给用户。
    @Published private(set) var deviceError: String?
    /// ★ 刷新请求是否已发出但还没回来。用来转圈 + 禁用按钮，
    ///    否则用户会连点、刷出一堆并发探测（隧道是**进程级单例**）。
    @Published private(set) var isRefreshingDevice = false
    /// 用户是否点过刷新。没点过时提示语不能说「未检测到」——
    /// 那听起来像设备真的不在，而事实是**我们还没问过**。
    @Published private(set) var hasQueriedDevice = false

    @Published private(set) var progressText = "尚未开始"

    /// ★ 会话进行中的实时读数（1 Hz 更新）。
    ///
    /// 2026-10-15 之前这里挂的是一个 `@Published var tick: (t,lat,lon,s)?`
    /// —— **从来没被赋过值**，是个死字段。而 Python 侧一直在按 1 Hz 发
    /// `tick` / `progress`，事件也一直在进 `apply(_:)`，只是被 `if` 滤掉了。
    /// 两边都不报错，所以这个洞可以潜伏到有人真去读实时数据才发现。
    /// 见 LiveMetrics.swift。
    @Published private(set) var live = LiveMetrics()

    /// 运行中锁滑块（§5.7）。只读，不阻止越界设定，只负责如实预警。
    var isSpeedLocked: Bool { phase.isActive }

    let route: RouteModel
    let sidecar: SidecarClient

    /// 日志出口。MainView 把它接到自己的 LogStore 上 ——
    /// 侧车的消息**一律经过 SessionController** 再出去，
    /// 这样「侧车 → UI」只有一条路径，日志才不会漏。
    var logSink: ((LogEntry) -> Void)?

    init(sidecar: SidecarClient, route: RouteModel) {
        self.sidecar = sidecar
        self.route = route
        // 自己当自己的 delegate：少一层转发，状态也不会分裂成两份
        sidecar.delegate = self

        // 打开界面就探一次。
        // 设备是热插拔的，用户插上手机后界面不会自己变 ——
        // 所以探一次只是把「首次状态」填上，之后仍靠用户点刷新按钮。
        refreshDeviceStatus()
    }

    private func log(_ level: LogLevel, _ text: String) {
        logSink?(.now(level, text))
    }

    // MARK: - 预览计算（纯本地，60 fps）

    /// 拖动中用它 —— 不等进程往返（works.md §5.6）。
    struct Preview {
        let paceStr: String
        let naiveStr: String
        let etaStr: String
        let sigmaV: Double
        let span: (lo: Double, hi: Double)
        let color: SafetyColor
        let verdict: [String: String]
        let band2: ClosedRange<Double>?
        let band1: ClosedRange<Double>?
    }

    func preview() -> Preview {
        let dt = 1.0 / max(sampleRate, 0.01)
        let sigma = PreviewMath.sigmaApparent(dt: dt, level: noise)
        let eta = PreviewMath.closedFormEta(distanceM: targetDistanceM, kmh: speedKmh)
        return Preview(
            paceStr: PreviewMath.paceText(PreviewMath.pace(kmh: speedKmh)),
            naiveStr: PreviewMath.durationText(PreviewMath.naiveEta(distanceM: targetDistanceM, kmh: speedKmh)),
            etaStr: PreviewMath.durationText(eta),
            sigmaV: sigma,
            span: PreviewMath.velocitySpan(kmh: speedKmh, sigmaV: sigma),
            color: PreviewMath.color(kmh: speedKmh, sigmaV: sigma),
            verdict: PreviewMath.verdict(distanceM: targetDistanceM, kmh: speedKmh, etaS: eta, sigmaV: sigma),
            band2: PreviewMath.band(sigmaV: sigma, levels: 2),
            band1: PreviewMath.band(sigmaV: sigma, levels: 1)
        )
    }

    /// ★ 滑块 ↔ 配速的双向映射（§5.2）。
    /// 默认配速线性：每一格代表相同的「配速感受」，因为跑步者的心智单位是配速。
    func speedForPace(_ secondsPerKm: Double) -> Double {
        paceLinear ? 3600.0 / secondsPerKm : secondsPerKm
    }

    /// 滑块 0…1 的归一化位置（两种映射共用同一根滑条）。
    /// 映射规则本体在 `PreviewMath`，这里只注入当前开关值。
    func sliderFraction(for speed: Double) -> Double {
        PreviewMath.sliderFraction(for: speed, paceLinear: paceLinear)
    }

    func speedForFraction(_ f: Double) -> Double {
        PreviewMath.speedForFraction(f, paceLinear: paceLinear)
    }

    // MARK: - 侧车交互

    func refreshEstimate() {
        sidecar.send(.estimate(speed: speedKmh, distance: targetDistanceM,
                               noise: noise, sampleRate: sampleRate))
    }

    func apply(_ result: EstimateResult) {
        exact = result
        // ★ 差值 > 1 s 是**自检信号**，不是错误（§5.6）
        driftSeconds = abs(result.etaS - PreviewMath.closedFormEta(distanceM: targetDistanceM, kmh: speedKmh))
    }

    func apply(_ ev: SidecarEvent) {
        // ★ 实时指标先接。`ingest` 自己判别 `progress` 是摘要还是实时，
        //   也会顺带返回「这条事件有没有更新读数」——
        //   所以下面那个 `device` 分支不受影响，两条线互不干扰。
        if live.ingest(ev) { return }

        if ev.event == "device" {
            // 每次收到 device 事件都收尾查询状态 ——
            // 否则刷新按钮会一直转圈到超时，用户会以为按钮坏了。
            hasQueriedDevice = true
            finishDeviceQuery()

            deviceConnected = ev.connected ?? false
            deviceName = ev.deviceName
            deviceError = ev.error

            // ★ 把设备位置同步给 RouteModel，绿圈才有东西可画。
            //   `lat`/`lon` 必须**成对**才采信（见 SidecarEvent.location）。
            route.deviceConnected = deviceConnected
            route.deviceLocation = deviceConnected ? ev.location : nil
        }
    }

    // MARK: - 设备状态刷新

    /// ★ 主动问一次侧车「iPhone 在不在」—— 用户新增的按钮。
    ///
    /// 为什么要这个按钮（`user_found_problem.md` 第 2 条）
    ///     设备是**热插拔**的：用户没插手机时打开 GUI，
    ///     侧车起来那一刻探测不到，之后插上线界面也不会自己变。
    ///     没有这个入口，用户只能重启 GUI —— 而重启的代价比点一下大得多。
    ///
    /// ⚠️ 防连点是必须的，不只是为了 UI 观感：
    ///     RSD 隧道是**进程级单例**，并发探测会互相打架。
    ///     侧车那边共用同一个 bridge，重复 connect 会被去重，
    ///     但按钮连点仍然会刷出一串无意义的请求与日志。
    func refreshDeviceStatus() {
        guard !isRefreshingDevice else { return }
        isRefreshingDevice = true
        sidecar.send(.deviceStatus())
        // 侧车不回就永远转圈 ⇒ 必须有兜底。10 s 足够覆盖
        // 「建隧道 + 开 DTX 通道」的真实耗时（M0 实测在 3 s 以内）。
        refreshTimeoutTask?.cancel()
        refreshTimeoutTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard !Task.isCancelled else { return }
            self?.finishDeviceQuery(note: "设备状态查询超时（10 s）——试试拔插一次 USB，或看侧车日志")
        }
    }

    private var refreshTimeoutTask: Task<Void, Never>?

    private func finishDeviceQuery(note: String? = nil) {
        refreshTimeoutTask?.cancel()
        refreshTimeoutTask = nil
        isRefreshingDevice = false
        if let note { deviceError = note }
    }

    // MARK: - 会话控制

    func prepare() {
        // ⚠️ 必须清：否则上一次会话残留的距离会一直挂在 HUD 上，
        //   新会话刚开始的十几秒里用户会以为「怎么一上来就走了 2 km」。
        //
        //   刻意放在 guard **之前** —— 用户按下「预置」就意味着「我要重开一次」，
        //   此时哪怕因为点数不足而被拒，界面也不该还显示上一次的距离。
        //   （stop 冻结是为了核对终点；prepare 是开始新一轮，语义相反。）
        live.reset()
        guard route.isRoutable else { progressText = "路线点数不足"; return }
        progressText = "预置起点…"
        phase = .prepared
        sidecar.send(.routeSet(route.points.map(\.pair)))
        sidecar.send(.command(Command.Name.runPrepare))
    }

    func start() {
        guard !isSpeedLocked else { return }
        refreshEstimate()
        progressText = "开始推进…"
        phase = .running
        sidecar.send(.command(Command.Name.runStart))
    }

    func pause() {
        phase = .paused
        sidecar.send(.command(Command.Name.runPause))
    }

    func resume() {
        guard phase == .paused else { return }
        phase = .running
        sidecar.send(.command(Command.Name.runResume))
    }

    func stop() {
        phase = .stopped
        // 冻结而不清零 —— 用户要靠它核对「到底停在哪儿、走了多远」。
        live.freeze()
        progressText = "已停止（保持在终点）"
        sidecar.send(.command(Command.Name.runStop))
    }

    /// 显式清理。这是**唯一**该发出清理的地方 —— 杜绝多处调用导致分叉。
    func cleanup() {
        sidecar.send(.deviceCleanup())
        live.reset()
        phase = .cleaned
        progressText = "已清除模拟定位"
    }
}

extension Command {
    /// 零载荷命令（`run.pause` / `run.stop` …）。
    static func command(_ name: String) -> Command {
        Command(command: name, payload: nil, text: nil, points: nil)
    }
}

// ══════════════════════════════════════════════════════════════════
//  侧车回调
//
//  SessionController 自己当 SidecarClient 的 delegate ——
//  「侧车 → 状态 → 界面」只有这一条路径。绕过它直接改 @Published
//  的地方越多，状态分裂的风险越大。
// ══════════════════════════════════════════════════════════════════

extension SessionController: SidecarClientDelegate {

    nonisolated func sidecar(_ c: SidecarClient, didReceive event: SidecarEvent) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.apply(event)
            if event.event == "device" {
                self.log(event.connected == true ? .success : .info,
                         event.connected == true ? "设备已连接" : "设备未连接（面板预览仍可用）")
            }
        }
    }

    nonisolated func sidecar(_ c: SidecarClient, didReceive result: EstimateResult) {
        Task { @MainActor [weak self] in
            self?.apply(result)
        }
    }

    nonisolated func sidecar(_ c: SidecarClient, didLog entry: LogEntry) {
        Task { @MainActor [weak self] in
            self?.logSink?(entry)
        }
    }

    nonisolated func sidecar(_ c: SidecarClient, didChangeState state: SidecarClient.State) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            switch state {
            case .running:      self.log(.success, "侧车已连接 —— 面板可离线使用")
            case .stopped:      self.log(.info, "侧车已关闭")
            case .starting:     self.log(.info, "正在启动侧车…")
            case .failed(let m): self.log(.error, "侧车不可用：\(m)")
            }
        }
    }
}
