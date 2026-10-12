import SwiftUI
import CoreLocation

// ══════════════════════════════════════════════════════════════════
//  ProbeApp —— 生死验证 2 的探针
//
//  它只做一件事：**如实显示**当前 CLLocation 的关键字段。
//  唯一要回答的问题是 `isSimulatedBySoftware` 是不是 true。
//
//  ★ 为什么必须用独立探针，而不能去看校园 App
//    works.md §8 R-1：若注入的位置被标记为「软件模拟」，而校园 App 又检测
//    这个字段，整个方案的路线选择就失效了。
//    但校园 App 是**黑盒** —— works.md §0 明确「不分析、不反编译、不 hook」。
//    所以只能自己写一个**看得见这些字段**的 App，从外部观察。
//
//  ★ 它同时解决了第二个难题
//    「怎么确认注入真的生效」原本没有低成本手段 —— 有了这个探针，
//    只要注入前后对比一次屏幕就够了，不需要碰校园 App 一根手指头。
//
//  —— 本文件属于 M0，不属于 ①②③④ 任何一层：它是**观测工具**，不参与产品逻辑。
// ══════════════════════════════════════════════════════════════════

@main
struct ProbeApp: App {
    var body: some Scene {
        WindowGroup {
            ProbeView()
        }
    }
}

// ──────────────────────────────────────────────────────────────────
//  观测模型
// ──────────────────────────────────────────────────────────────────

@MainActor
final class ProbeModel: NSObject, ObservableObject, CLLocationManagerDelegate {

    @Published private(set) var samples: [Line] = []
    @Published private(set) var authorized = false
    @Published private(set) var latestSimulated: Bool?

    private let manager = CLLocationManager()

    /// 界面上的一行。刻意用纯文本而不是格式化组件 ——
    /// 探针的可信度来自「一眼看懂」，任何美化都可能掩盖字段的真实取值。
    struct Line: Identifiable {
        let id = UUID()
        let text: String
    }

    override init() {
        super.init()
        manager.delegate = self
        // ★ 要的是原始字段，用最高精度、不过滤的授权级别。
        // reducedAccuracy / 模糊定位都会影响其它字段的观察。
        manager.desiredAccuracy = kCLLocationAccuracyBestForNavigation
        manager.requestWhenInUseAuthorization()
        // 先不开定位，等授权回调后再开，避免首帧拿不到数据。
    }

    func start() {
        samples = [Line(text: "请求定位中……")]
        manager.startUpdatingLocation()
    }

    // ── CLLocationManagerDelegate ──────────────────────────────

    nonisolated func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        Task { @MainActor in
            authorized = (m.authorizationStatus == .authorizedWhenInUse
                          || m.authorizationStatus == .authorizedAlways)
            if authorized { start() }
            else { samples = [Line(text: "未获定位授权：\(m.authorizationStatus.rawValue)")] }
        }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        // 只取最新一个 —— 探针要回答的是「现在这个字段是什么」，
        // 不是历史统计。历史数据在侧车的 CSV 里。
        guard let loc = locs.last else { return }
        let rows = Self.describe(loc)
        let flag = loc.sourceInformation?.isSimulatedBySoftware
        Task { @MainActor in
            latestSimulated = flag
            samples = rows
        }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didFailWithError err: Error) {
        Task { @MainActor in samples = [Line(text: "定位失败：\(err.localizedDescription)")] }
    }

    // ── 字段格式化 ────────────────────────────────────────────

    /// 把一个 CLLocation 拆成可读的行。顺序固定，便于前后对比。
    nonisolated static func describe(_ loc: CLLocation) -> [Line] {
        let simFlag = loc.sourceInformation?.isSimulatedBySoftware
        return [
            Line(text: "──────────── 观测 ────────────"),
            Line(text: "isSimulatedBySoftware : \(simFlag.map(String.init) ?? "nil")"),
            Line(text: "          （nil = 系统未提供该字段）"),
            Line(text: ""),
            Line(text: "latitude   : \(String(format: "%.7f", loc.coordinate.latitude))"),
            Line(text: "longitude  : \(String(format: "%.7f", loc.coordinate.longitude))"),
            Line(text: "altitude   : \(String(format: "%.2f", loc.altitude)) m"),
            Line(text: "horizontalAccuracy : \(String(format: "%.1f", loc.horizontalAccuracy)) m"),
            Line(text: "speed      : \(String(format: "%.3f", loc.speed)) m/s"),
            Line(text: "course     : \(String(format: "%.1f", loc.course)) °"),
            Line(text: "timestamp  : \(loc.timestamp.formatted())"),
            Line(text: ""),
            Line(text: "──────────── 判定 ────────────"),
            Line(text: judge(simFlag)),
        ]
    }

    /// 把布尔字段翻译成一句人话结论 —— 这是探针存在的意义所在。
    private nonisolated static func judge(_ value: Bool?) -> String {
        guard let value else {
            return "无法判定：字段为 nil\n（可能未开启开发者模式，或系统版本不暴露该字段）"
        }
        return value
            ? "⚠️ 被标记为「软件模拟」\n若校园 App 检测此字段，本方案路线失效"
            : "✅ 未被标记为「软件模拟」\n校园 App 大概率读不到模拟痕迹"
    }
}

// ──────────────────────────────────────────────────────────────────
//  界面
// ──────────────────────────────────────────────────────────────────

struct ProbeView: View {
    @StateObject private var model = ProbeModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(model.samples) { line in
                        Text(line.text)
                            .font(.system(size: 13, design: .monospaced))
                            .foregroundStyle(color(for: line.text))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(12)
            }
        }
        .task { if model.authorized { model.start() } }
        .onChange(of: model.authorized) { _, ok in if ok { model.start() } }
    }

    private var header: some View {
        HStack {
            Text("VRun 探针")
                .font(.headline)
            Spacer()
            if let flag = model.latestSimulated {
                Text(flag ? "SIMULATED" : "CLEAN")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(flag ? Color.red : Color.green, in: Capsule())
                    .foregroundStyle(.white)
            }
        }
        .padding(12)
    }

    /// 只给关键行上色，其余保持中性 —— 避免满屏颜色干扰判读。
    private func color(for text: String) -> Color {
        if text.contains("isSimulatedBySoftware") || text.hasPrefix("⚠️") || text.hasPrefix("✅") {
            return text.contains("true") ? .red : (text.hasPrefix("✅") ? .green : .primary)
        }
        return .primary
    }
}
