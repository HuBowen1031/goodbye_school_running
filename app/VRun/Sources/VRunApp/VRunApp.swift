import SwiftUI
import AppKit

// ══════════════════════════════════════════════════════════════════
//  VRunApp.swift —— @main 入口
//
//  ⚠️ 仓库根目录怎么找（findings.md F7 的同源问题）
//     侧车路径已由 `scripts/resolve_python.sh` 解析；这里同理 ——
//     依次尝试「App 内部 resources」→「可执行文件的上两级」→ 当前工作目录。
//     找不到就**明确报错**，不要静默用一个猜测的路径。
//
//  ⚠️ 启动时主动 cleanup 一次（file-manifest §5.2）
//     GUI 被强杀时无法保证清理。新一轮启动先把上一次崩溃残留的
//     虚拟定位扫掉 —— 否则博文的 iPhone 会一直卡在校园那个点上。
// ══════════════════════════════════════════════════════════════════

@main
struct VRunApp: App {

    @StateObject private var sidecar: SidecarClient

    init() {
        let root = MainView.locateRepoRoot()
        _sidecar = StateObject(wrappedValue: SidecarClient(repoRoot: root))
    }

    var body: some Scene {
        Window("VRun", id: "main") {
            MainView(sidecar: sidecar)
                .frame(minWidth: 900, minHeight: 600)
                .onAppear {
                    // 兜底清理：把上次异常退出残留的模拟定位扫掉
                    sidecar.send(.deviceCleanup())
                }
        }
        .defaultSize(width: 1240, height: 820)
        // ⚠️ 隐藏标题栏（只隐藏标题**文字**，红绿灯与拖拽区仍在）。
        //    原因：顶部要放「侧栏开合按钮」，而系统标题的占位宽度会随窗口宽度
        //    变化，按钮无论摆在红绿灯后还是固定 x 都会和标题撞上。
        //    隐藏之后整条顶部由我们独占，按钮位置才是确定的。
        .windowStyle(.hiddenTitleBar)
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {}   // 单窗口应用，不给"新建"
        }
    }
}

extension MainView {
    /// 定位仓库根目录（含 `scripts/resolve_python.sh` 的那一层）。
    ///
    /// ⚠️ **为什么必须有 `VRUN_REPO_ROOT`**（2026-10-13 实测踩到）：
    /// 为了绕开 `~/Documents` 的 iCloud xattr 污染（works.md §6 M5），
    /// SwiftPM 的构建目录被指到 `--scratch-path /tmp/vrun-swift`，
    /// 于是可执行文件**根本不在仓库里面**，从它往上找 6 层也到不了仓库根；
    /// 而 `open -a` 启动时 cwd 又是 `/`。
    /// ⇒ 两条自然路径全废，repoRoot 退化成 `/`，侧车报
    ///   「找不到 /scripts/resolve_python.sh」。
    /// 环境变量是**唯一可靠**的开发态入口：打包后走 Bundle 资源那条即可。
    static func locateRepoRoot() -> URL {
        let marker = "scripts/resolve_python.sh"
        var candidates: [URL] = []

        // ⓪ 开发态显式指定 —— 优先级最高
        if let env = ProcessInfo.processInfo.environment["VRUN_REPO_ROOT"] {
            candidates.append(URL(fileURLWithPath: (env as NSString).expandingTildeInPath))
        }

        // ① .app 打包后：VRun.app/Contents/Resources/repo 符号链接（build_app.sh 会建）
        if let res = Bundle.main.resourceURL?.appendingPathComponent("repo") {
            candidates.append(res)
        }

        // ② 开发态：可执行文件上溯（仅当构建目录在仓库内才有效）
        if let exe = Bundle.main.executableURL {
            candidates.append(exe.deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent())
        }
        // ③ 从当前目录上溯
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath))

        for base in candidates.map({ $0.standardizedFileURL }) {
            var d = base
            for _ in 0..<6 {
                let probe = d.appendingPathComponent(marker)
                if FileManager.default.isExecutableFile(atPath: probe.path) { return d }
                let up = d.deletingLastPathComponent().standardizedFileURL
                if up == d { break }          // 到根了就停，别在 "/" 上死循环
                d = up
            }
        }

        // 找不到也要能启动 —— 侧车会在 start() 里给出明确错误，而不是崩在这里
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }
}
