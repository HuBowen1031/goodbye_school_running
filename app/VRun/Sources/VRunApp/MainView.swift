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
        _session = StateObject(wrappedValue: SessionController(sidecar: sidecar))
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
            route: session.route,
            onPick: { p in
                session.route.add(p)
                log(.info, String(format: "加点 (%.6f, %.6f) · 共 %d 点 · %.0f m",
                                  p.lat, p.lon, session.route.points.count,
                                  session.route.totalLengthM))
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
                session.route.undo()
            }
            toolButton("checkmark", tip: "结束绘制") {
                isPenActive = false
                session.route.finish()
            }
            toolButton("trash", tip: "清空路线") {
                session.route.clear()
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

    /// 左上角提示：钢笔的用法 + 绿圈的含义
    private var mapHints: some View {
        VStack(alignment: .leading, spacing: 5) {
            if isPenActive {
                Label("点一下地图落一个点 · Esc 收笔 · ⌘Z 撤销", systemImage: "hand.tap")
                    .font(.system(size: 10))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .glassEffect(.regular.tint(.accentColor.opacity(0.3)), in: .capsule)
            }
            if session.route.points.isEmpty {
                Label("绿圈 = iPhone 当前位置，请从那里起笔", systemImage: "mappin.circle")
                    .font(.system(size: 10))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .glassEffect(.regular.tint(.safeGreen.opacity(0.3)), in: .capsule)
            }
        }
        .padding(12)
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
            KV(k: "路线", v: String(format: "%d 点 · %.0f m",
                                   session.route.points.count, session.route.totalLengthM),
               color: session.route.isRoutable ? .primary : .secondary)
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
        guard session.route.scaled(to: session.targetDistanceM) else {
            log(.warn, "点数不足，无法缩放（至少 2 个点）"); return
        }
        log(.success, String(format: "路线已缩放到 %.0f m", session.route.totalLengthM))
    }

    private func log(_ level: LogLevel, _ text: String) {
        logStore.append(.now(level, text))
    }
}

