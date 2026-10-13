import Foundation

// ══════════════════════════════════════════════════════════════════
//  Models.swift —— IPC 消息的 Codable 结构
//
//  ⚠️ 这些字段名必须与 `src/vrun/ipc.py` **逐字对应**。
//     改任何一边都要改另一边 —— 否则失败会表现为「字段全是 nil」，
//     而 JSON 解码错误通常只报一句 typeMismatch，极难定位。
//
//  权威定义见 works.md §4.2；实现见 docs/file-manifest.md §4.3.1。
// ══════════════════════════════════════════════════════════════════

// MARK: - 噪声档位（works.md §3.4）

/// 与 Python 侧 `GPSNoise.LEVELS` 的键一一对应。
/// **中文只出现在 SwiftUI 层**，不进入协议 —— 避免同一份中文字符串散落两侧。
enum NoiseLevel: String, Codable, CaseIterable, Identifiable {
    case off, realistic, harsh

    var id: String { rawValue }

    var label: String {
        switch self {
        case .off:       return "关"
        case .realistic: return "真实"
        case .harsh:     return "严苛"
        }
    }

    /// 相对基准噪声的缩放系数（Python 侧同名常量）。
    var scale: Double {
        switch self {
        case .off:       return 0.0
        case .realistic: return 1.0
        case .harsh:     return 1.5
        }
    }
}

// MARK: - 命令（GUI → 侧车）

/// 一条发往侧车的命令。`command` 字段是分派键。
struct Command: Encodable {
    let command: String
    var payload: [String: Double]?
    var text: [String: String]?
    var points: [[Double]]?

    enum Name {
        static let routeSet      = "route.set"
        static let runPrepare    = "run.prepare"
        static let runStart      = "run.start"
        static let runPause      = "run.pause"
        static let runResume     = "run.resume"
        static let runStop       = "run.stop"
        static let deviceCleanup = "device.cleanup"
        static let deviceStatus  = "device.status"
        static let estimate      = "estimate"
    }

    // 固定编码键名，避免 Swift 字典键顺序带来的无谓差异
    enum CodingKeys: String, CodingKey {
        case command, payload, text, points
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(command, forKey: .command)
        try c.encodeIfPresent(payload, forKey: .payload)
        try c.encodeIfPresent(text, forKey: .text)
        try c.encodeIfPresent(points, forKey: .points)
    }

    /// 面板预览。**不触碰设备**（works.md §5.7）。
    static func estimate(speed: Double, distance: Double,
                         noise: NoiseLevel, sampleRate: Double, seed: Int = 0) -> Command {
        Command(command: Name.estimate, payload: [
            "speed": speed, "distance": distance,
            "sample_rate": sampleRate, "seed": Double(seed),
        ], text: ["noise": noise.rawValue], points: nil)
    }

    static func deviceStatus() -> Command { Command(command: Name.deviceStatus) }
    static func deviceCleanup() -> Command { Command(command: Name.deviceCleanup) }

    static func routeSet(_ points: [[Double]]) -> Command {
        Command(command: Name.routeSet, payload: nil, text: nil, points: points)
    }
}

// MARK: - 事件（侧车 → GUI）

/// 侧车发回的 `estimate.result`。
///
/// 字段与 `ipc.py::_handle_estimate` 的 event() 一一对应。
struct EstimateResult: Decodable, Equatable {
    let speedKmh: Double
    let distanceM: Double
    let noiseLevel: String
    let sampleRate: Double

    let paceSPerKm: Double
    let paceStr: String
    let etaNaiveS: Double
    let etaS: Double
    let etaClosedFormS: Double
    let etaStr: String

    let sigmaV: Double
    let band1Sigma: [Double]?
    let band2Sigma: [Double]?
    let safety: String
    let vLo: Double
    let vHi: Double
    let clampEnabled: Bool
    let verdict: [String: String]

    enum CodingKeys: String, CodingKey {
        case speedKmh = "speed_kmh"
        case distanceM = "distance_m"
        case noiseLevel = "noise_level"
        case sampleRate = "sample_rate"
        case paceSPerKm = "pace_s_per_km"
        case paceStr = "pace_str"
        case etaNaiveS = "eta_naive_s"
        case etaS = "eta_s"
        case etaClosedFormS = "eta_closed_form_s"
        case etaStr = "eta_str"
        case sigmaV = "sigma_v"
        case band1Sigma = "band_1sigma"
        case band2Sigma = "band_2sigma"
        case safety
        case vLo = "v_lo"
        case vHi = "v_hi"
        case clampEnabled = "clamp_enabled"
        case verdict
    }

    var noise: NoiseLevel { NoiseLevel(rawValue: noiseLevel) ?? .realistic }
    var color: SafetyColor { SafetyColor(rawValue: safety) ?? .red }
}

/// 安全带三级判定（works.md §5.3）。
enum SafetyColor: String {
    case green, yellow, red

    var label: String {
        switch self {
        case .green:  return "安全"
        case .yellow: return "临界"
        case .red:    return "风险"
        }
    }
}

/// 侧车发回的通用事件。
struct SidecarEvent: Decodable {
    let event: String
    let code: String?
    let message: String?
    let hint: String?
    let connected: Bool?
    let ddiMounted: Bool?
    let streaming: Bool?

    enum CodingKeys: String, CodingKey {
        case event, code, message, hint
        case connected
        case ddiMounted = "ddi_mounted"
        case streaming
    }
}

// MARK: - 坐标

/// 地图上的一个点。经纬度 + 名字，够 RouteCanvas 与 Sidecar 两条线共用。
struct GeoPoint: Codable, Identifiable, Equatable {
    var id = UUID()
    var lat: Double
    var lon: Double
    var title: String?

    init(lat: Double, lon: Double, title: String? = nil) {
        self.lat = lat
        self.lon = lon
        self.title = title
    }

    var pair: [Double] { [lon, lat] }   // GeoJSON 惯例：lon 在前
}

// MARK: - 日志（草图左下面板）

/// 一条日志。`day` 单独存一份，让「按天筛选」不必去解析时间字符串。
enum LogLevel: String, Codable, CaseIterable {
    case info, success, warn, error, debug

    var label: String {
        switch self {
        case .info:    return "信息"
        case .success: return "成功"
        case .warn:    return "警告"
        case .error:   return "错误"
        case .debug:   return "调试"
        }
    }

    var symbol: String {
        switch self {
        case .info:    return "●"
        case .success: return "✓"
        case .warn:    return "▲"
        case .error:   return "✕"
        case .debug:   return "·"
        }
    }
}

struct LogEntry: Identifiable, Equatable {
    let id = UUID()
    let date: Date
    let level: LogLevel
    let text: String

    /// 本地时区的「天」，形如 2026-10-13。草图要求的按天筛选依赖它。
    var day: String {
        LogEntry.dayFormatter.string(from: date)
    }

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    static func now(_ level: LogLevel, _ text: String) -> LogEntry {
        LogEntry(date: Date(), level: level, text: text)
    }
}
