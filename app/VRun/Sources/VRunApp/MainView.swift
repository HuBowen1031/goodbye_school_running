import SwiftUI
import MapKit

// ══════════════════════════════════════════════════════════════════
//  MainView.swift —— 窗口布局（依草图）
//
//  ┌──────────────┬────────────────────────────────────────────┐
//  │ 操作/调整面板 │                                            │
//  │ （滑块在这里）│              Map View                      │ ║钢笔│
//  ├──────────────┤                        ● 从 iPhone 当前位置  │ ║工具│
//  │ 可滚动日志   │                          起笔              │ ║栏  │
//  │ （彩色/按天）│                          ┌──────────────┐  │ ║    │
//  │              │                          │ 侧车状态浮窗 │  │      │
//  └──────────────┴──────────────────────────┴──────────────┴──┘
//
//  两处与 works.md §5.1 的差异（都是草图明确要求的）：
//   ① 面板**可调宽度**且可折叠 —— 草图标了「DK 可调面板」与悬浮按钮
//   ② 日志面板独立成块并支持**按天筛选** —— §5.1 没有这一块
// ══════════════════════════════════════════════════════════════════

struct MainView: View {

    @StateObject private var session: SessionController
    @StateObject private var logStore = LogStore()

    /// ★ 路线模型必须由 MainView **直接**持有，不能只挂在 SessionController 里。
    ///
    /// 2026-10-14 实测踩到：原先 `route` 是 `SessionController` 的一个 `let` 属性，
    /// 于是 MainView 只观察 SessionController 的 `objectWillChange`，
    /// **没有任何人在听 RouteModel 的**。
    ///
    /// 后果是点一下地图后 `route.points.count` 确实变成 1（日志可证），
    /// 但界面**完全不重绘** —— HUD 永远显示「0 点」，
    /// 绿圈也只在「碰巧因为别的原因重绘」时才出现。
    ///
    /// ★ 这个 bug 极其隐蔽：
    ///   · 数据是对的，看日志一切正常
    ///   · 编译零警告
    ///   · 有时又「看起来能用」，所以极难归因
    ///   而它和「地图点击没反应」「加点闪退」叠加在一起时，
    ///     症状几乎一模一样 —— 全都指向「地图不响应」。
    ///
    /// 修法：RouteModel 提升为 MainView 的 @StateObject，
    /// 再注入给 SessionController —— **两边都在观察它**。
    @StateObject private var route: RouteModel

    @State private var sidebarWidth: CGFloat = 340
    @State private var sidebarVisible = true
    @State private var isPenActive = false
    @State private var mapRef: MKMapView?

    private let minW: CGFloat = 280, maxW: CGFloat = 460

    /// ★ 统一工具栏占用的高度（实测 37 pt：红绿灯中心约在 17 pt，
    ///   内容区从 37 pt 往下开始）。
    ///   macOS 的内容视图**延伸到标题栏底下**，所以要让按钮和红绿灯同排，
    ///   必须用负偏移把它提上去。这是本窗口唯一的「不可见但占位」区域。
    private static let titleBarInset: CGFloat = 37
    /// 红绿灯三个按钮加上左右留空的宽度，按钮排在它们右边。
    private static let trafficLightTrailing: CGFloat = 76

    init(sidecar: SidecarClient) {
        // 先建 route，再把**同一个实例**注入 SessionController ——
        // 不能各建一个，否则界面和会话看到的是两份路线。
        let r = RouteModel()
        _route = StateObject(wrappedValue: r)
        _session = StateObject(wrappedValue: SessionController(sidecar: sidecar, route: r))
    }

    var body: some View {
        GlassEffectContainer(spacing: 12) {
            HStack(spacing: 0) {
                if sidebarVisible { sidebar }
                divider
                mapArea
            }
            .frame(minWidth: 900, minHeight: 600)
            .background(.regularMaterial)
            // ★ 侧栏开合按钮挂在**根视图**上而不是地图上：
            //   · 展开时贴着侧栏右缘 —— 拖分隔条调宽时它跟着跑
            //   · 收起时滑到红绿灯右边
            //   · 用负偏移提到标题栏条里，才能真正和红绿灯同排
            .overlay(alignment: .topLeading) {
                floatingToggle
                    .offset(x: sidebarVisible ? max(sidebarWidth - toggleSize - 10, 10)
                                            : Self.trafficLightTrailing,
                            y: 4 - Self.titleBarInset)
            }
        }
        .onAppear { bootstrap() }
        .onReceive(Timer.publish(every: 0.25, on: .main, in: .common).autoconnect()) { _ in
            // 250 ms 去抖：松手后才向侧车要精确值（§5.6）
            if needsCommit { commitToSidecar() }
        }
    }

    private let toggleSize: CGFloat = 26

    // ══════════════════════════════════════════════════════
    //  左侧面板
    // ══════════════════════════════════════════════════════

    private var sidebar: some View {
        VStack(spacing: 10) {
            // ★ 设备状态卡放在最上面：它决定「能不能开始画」，
            //   埋在下面的话用户第一眼看不到，也就不会去按刷新。
            GlassPanel(cornerRadius: 16) {
                DeviceStatusCard(session: session)
            }
            .frame(height: deviceCardHeight)

            GlassPanel(cornerRadius: 16) {
                SpeedPanel(session: session) { commitToSidecar() }
            }
            .frame(height: max(360, sidebarHeight(for: .panel)))

            GlassPanel(cornerRadius: 16) {
                LogPanel(store: logStore)
            }
            .frame(maxHeight: .infinity)
        }
        .padding(10)
        .frame(width: sidebarWidth)
        .transition(.move(edge: .leading).combined(with: .opacity))
    }

    private enum SidebarSlot { case panel }
    private func sidebarHeight(for slot: SidebarSlot) -> CGFloat { 430 }
    private var deviceCardHeight: CGFloat { 92 }

    // ══════════════════════════════════════════════════════
    //  分隔条 —— 拖动调宽（草图：DK 可调面板）
    // ══════════════════════════════════════════════════════

    private var divider: some View {
        ZStack {
            Rectangle()
                .fill(.clear)
                .frame(width: 10)
                .contentShape(Rectangle())
                .onHover { inside in
                    if inside { NSCursor.resizeLeftRight.push() } else { NSCursor.pop() }
                }
                .gesture(
                    DragGesture()
                        .onChanged { g in
                            sidebarWidth = min(max(sidebarWidth + g.translation.width, minW), maxW)
                        }
                )
            // 细握把，视觉上提示「可拖」
            Capsule()
                .fill(Color.secondary.opacity(0.35))
                .frame(width: 3, height: 36)
        }
        .frame(width: 10)
    }

    // ══════════════════════════════════════════════════════
    //  地图区
    // ══════════════════════════════════════════════════════

    /// 地图区。四个 overlay 各占一角，互不重叠 ——
    /// 钢笔工具栏竖排在**右侧居中**，状态浮窗在**右下**（草图要求），
    /// 提示在左上，开合按钮在右上。
    private var mapArea: some View {
        RouteCanvas(
            isPenActive: isPenActive,
            route: route,
            onPick: { p in
                route.add(p)
                log(.info, String(format: "加点 (%.6f, %.6f) · 共 %d 点 · %.0f m",
                                  p.lat, p.lon, route.points.count,
                                  route.totalLengthM))
            }
        )
        .ignoresSafeArea()
        // 草图右侧的竖排工具栏（钢笔）
        .overlay(alignment: .trailing) { penToolbar.padding(.trailing, 14) }
        // 草图右下角的小浮窗
        .overlay(alignment: .bottomTrailing) { statusHUD.padding(14) }
        .overlay(alignment: .topLeading) { mapHints }
    }

    /// 钢笔工具栏 —— 「类似『钢笔』工具，不要求用户一笔画完」
    private var penToolbar: some View {
        VStack(spacing: 6) {
            toolButton("pencil.tip", active: isPenActive,
                       tip: "钢笔：点一下加一个点，可停笔再续") {
                isPenActive.toggle()
                log(.info, isPenActive ? "钢笔已开启 —— 在地图上点击落点" : "钢笔已收起")
            }
            toolButton("arrow.uturn.backward", tip: "撤销上一个点") {
                route.undo()
            }
            toolButton("checkmark", tip: "结束绘制") {
                isPenActive = false
                route.finish()
            }
            toolButton("trash", tip: "清空路线") {
                route.clear()
            }
            Divider().frame(width: 18).opacity(0.4)
            toolButton("arrow.left.and.right", tip: "按目标距离缩放") {
                scaleToTarget()
            }
        }
        .padding(7)
        .glassEffect(.regular, in: .rect(cornerRadius: 16, style: .continuous))
    }

    private func toolButton(_ icon: String, active: Bool = false,
                            tip: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .medium))
                .frame(width: 28, height: 28)
                .foregroundStyle(active ? Color.white : Color.primary)
                .background {
                    if active { Circle().fill(Color.accentColor) }
                }
        }
        .buttonStyle(.plain)
        .help(tip)
    }

    /// 左上角提示：钢笔的用法 + 起笔指引
    ///
    /// ★ 指引必须随设备状态改口（`user_found_problem.md` 第 1、2 条）
    ///   · 连上手机 → 绿：「从 iPhone 当前位置起笔」（绿圈在设备位置）
    ///   · 没连 / 没问过 → 红：「点一下地图作为起点」（绿圈会落在你点的第一点）
    /// 原文案是一句无条件成立的「绿圈 = iPhone 当前位置，请从那里起笔」——
    ///   手机没插时绿圈根本不会出现，用户照着做却**没法开始画**。
    private var mapHints: some View {
        VStack(alignment: .leading, spacing: 5) {
            if isPenActive {
                Label("点一下地图落一个点 · Esc 收笔 · ⌘Z 撤销", systemImage: "hand.tap")
                    .font(.system(size: 10))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .glassEffect(.regular.tint(.accentColor.opacity(0.3)), in: .capsule)
            }
            if route.points.isEmpty {
                let hint = startHint
                Label(hint.text, systemImage: hint.icon)
                    .font(.system(size: 10))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .glassEffect(.regular.tint(hint.color.opacity(0.3)), in: .capsule)
            }
        }
        .padding(12)
    }

    private struct StartHint {
        let text: String
        let icon: String
        let color: Color
    }

    /// 起笔指引。三种情形 —— 别把它们合并成一句，
    /// 「没问过」和「问了、没有」对用户是完全不同的处境。
    private var startHint: StartHint {
        if session.deviceConnected, route.deviceLocation != nil {
            return StartHint(text: "绿圈 = iPhone 当前上报位置，从那里起笔",
                             icon: "mappin.circle", color: .safeGreen)
        }
        if !session.hasQueriedDevice {
            return StartHint(text: "尚未检测设备 —— 点一下地图作为起点，插上手机后到左侧刷新",
                             icon: "iphone.slash", color: .warnYellow)
        }
        if session.deviceConnected {
            // 连上了但还没注入过：绿圈此刻在用户点上，这是正常的起步方式
            return StartHint(text: "尚未注入位置 —— 点一下地图作为起点（绿圈即起点）",
                             icon: "mappin.circle", color: .warnYellow)
        }
        return StartHint(text: "未检测到 iPhone —— 点一下地图作为起点，插上后到左侧点「刷新」",
                         icon: "iphone.slash", color: .riskRed)
    }

    /// 草图右下角：调用 vrun 的小面板，简要说明状态和进度
    private var statusHUD: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Circle()
                    .fill(sidecarDot)
                    .frame(width: 6, height: 6)
                Text("侧车").font(.system(size: 10, weight: .semibold))
                Spacer(minLength: 8)
                Text(session.phase.rawValue)
                    .font(.system(size: 9, weight: .medium))
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(sidecarDot.opacity(0.18)))
                    .foregroundStyle(sidecarDot)
            }
            Divider().opacity(0.35)
            KV(k: "设备", v: deviceHUDText,
               color: session.deviceConnected ? Color.safeGreen
                      : (session.hasQueriedDevice ? Color.riskRed : .secondary))
            KV(k: "路线", v: String(format: "%d 点 · %.0f m",
                                    route.points.count, route.totalLengthM),
               color: route.isRoutable ? .primary : .secondary)
            KV(k: "目标", v: String(format: "%.2f km @ %.1f km/h",
                                   session.targetDistanceM / 1000, session.speedKmh), color: .primary)
            KV(k: "预计", v: session.exact?.etaStr ?? PreviewMath.durationText(
                    PreviewMath.closedFormEta(distanceM: session.targetDistanceM, kmh: session.speedKmh)))
            KV(k: "进度", v: session.progressText, color: .secondary)
        }
        // ⚠️ glassEffect 只负责折射层，**不给内容留白**。
        //    不显式 padding 的话文字会直接压在玻璃边缘上，看起来像被裁了一刀。
        .padding(.horizontal, 13)
        .padding(.vertical, 11)
        .frame(width: 236)
        .glassEffect(.regular, in: .rect(cornerRadius: 14, style: .continuous))
    }

    private var sidecarDot: Color {
        switch session.phase {
        case .failed: return .riskRed
        case .running, .paused: return .safeGreen
        case .prepared: return .warnYellow
        default: return .secondary
        }
    }

    /// HUD 里的设备一行。三种说法要分开 —— 同上，别混。
    private var deviceHUDText: String {
        if session.isRefreshingDevice { return "检测中…" }
        if session.deviceConnected { return session.deviceName ?? "已连接" }
        if session.hasQueriedDevice { return "未连接" }
        return "尚未检测"
    }

    // ══════════════════════════════════════════════════════
    //  悬浮按钮：打开/收起操作和状态面板
    // ══════════════════════════════════════════════════════

    private var floatingToggle: some View {
        Button {
            withAnimation(.spring(response: 0.32, dampingFraction: 0.85)) {
                sidebarVisible.toggle()
            }
            log(.info, sidebarVisible ? "已展开操作和状态面板" : "已收起操作和状态面板")
        } label: {
            Image(systemName: sidebarVisible ? "sidebar.leading" : "sidebar.squares.leading")
                .font(.system(size: 13, weight: .medium))
                .frame(width: toggleSize, height: toggleSize)
        }
        .buttonStyle(.glass)
        .help(sidebarVisible ? "收起操作和状态面板" : "展开操作和状态面板")
        .accessibilityLabel(sidebarVisible ? "收起操作和状态面板" : "展开操作和状态面板")
    }

    // ══════════════════════════════════════════════════════
    //  启动
    // ══════════════════════════════════════════════════════

    private func bootstrap() {
        // 日志出口接到自己的 store（SessionController 是唯一的 delegate）
        session.logSink = { entry in logStore.append(entry) }
        session.sidecar.start()
        log(.info, "VRun 就绪")
        log(.info, String(format: "规则窗口 %.0f–%.0f km/h · 距离 %.1f–%.0f km",
                          Rules.vMin, Rules.vMax, Rules.dMinM / 1000, Rules.dMaxM / 1000))
        refreshEstimateSoon()
    }

    /// 松手 250 ms 后问侧车要精确值（§5.6）
    private func refreshEstimateSoon() { needsCommit = true }

    /// 去抖标志。必须用 `@State` —— 普通 `var` 在非 mutating 方法里改不动它。
    @State private var needsCommit = false

    private func commitToSidecar() {
        needsCommit = false
        session.refreshEstimate()
    }

    private func scaleToTarget() {
        guard route.scaled(to: session.targetDistanceM) else {
            log(.warn, "点数不足，无法缩放（至少 2 个点）"); return
        }
        log(.success, String(format: "路线已缩放到 %.0f m", route.totalLengthM))
    }

    private func log(_ level: LogLevel, _ text: String) {
        logStore.append(.now(level, text))
    }
}

