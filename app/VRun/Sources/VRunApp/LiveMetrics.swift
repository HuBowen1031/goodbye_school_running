import Foundation
import CoreLocation

// ══════════════════════════════════════════════════════════════════
//  LiveMetrics.swift —— 会话进行中的**实时读数**
//
//  ★ 为什么单独抽一个文件
//
//      2026-10-15 发现：`vrun.session` 一直按 1 Hz 发 `tick` + `progress`，
//      但 Swift 侧压根没接 —— `SidecarEvent` 没解码那些字段，
//      `SessionController.apply` 也只认 `device` 事件。
//      **事件收下了、内容全丢，两侧都不报错。**
//      后果：会话进行中 GUI 对 progress 是瞎的，M4 的
//      「比对预览 ETA 与实际用时」只能靠翻日志文本。
//
//      这个 bug 的可怕之处不是缺功能，是**它不响**。
//      所以：① 载荷形状由 tests/golden/session_events.json 锁死（跨语言契约），
//            ② 摄入逻辑抽成纯函数放这里，能被单测直接驱动。
//
//  ★ 单位（已对着 session.py 逐个核过，golden 里也有实数可对）
//
//      t          秒
//      s          米（沿路线已走弧长）
//      target_m   米
//      v_target   km/h
//      v_actual   km/h（含噪声位移的影响，不是理论值）
//      sigma_v    km/h（表观速度 1σ）
//      sample_rate Hz
//
//  ⚠️ `progress` 事件名被**重载**了（协议层面的既有 wart）
//
//      `run.prepare` / `run.start` / `run.stop` 的应答，
//      与 1 Hz 的实时指标，用的是**同一个事件名** `progress`，
//      载荷形状却不同（前者是会话摘要，没有 `sigma_v`）。
//
//      所以摄入时**不能只看事件名**，必须看实时特征字段在不在。
//      否则 GUI 会把「会话刚开始，这是摘要」当成一条实时读数，
//      显示出一个不动的 0 km/h —— 比不显示更难查。
//      这个约定由 Python 侧 `test_progress确实被重载…` 守着。
// ══════════════════════════════════════════════════════════════════

struct LiveMetrics: Equatable {

    /// 已用时（秒）
    var elapsedS: Double = 0
    /// 沿路线已走弧长（米）
    var walkedM: Double = 0
    /// 路线全长（米）。`tick` 不带，只有 `progress` 带。
    var targetM: Double = 0
    /// 本段实际速度（km/h）
    var vActualKmh: Double = 0
    /// 剖面目标速度（km/h）
    var vTargetKmh: Double = 0
    /// 表观速度 1σ（km/h）
    var sigmaV: Double = 0
    /// 采样率（Hz）
    var sampleRate: Double = 0
    /// 当前位置
    var coordinate: GeoPoint?
    /// 是否收到过实时数据。**没收到过就不要显示 0** ——
    /// 那是「还没开始」，不是「跑了 0 米」。
    var isLive: Bool = false

    // MARK: - 摄入

    /// 吃一个侧车事件。**返回是否真的更新了实时读数**。
    ///
    /// - Returns: `false` 表示这条事件与实时读数无关（`device` / 会话摘要）。
    @discardableResult
    mutating func ingest(_ ev: SidecarEvent) -> Bool {
        switch ev.event {
        case "tick":
            // tick 只有 6 个字段，缺 target_m / sigma_v —— 保留上一条 progress 的值，
            // 不要用 0 覆盖（那会让 HUD 上的「路线全长」在两个 tick 之间来回跳）。
            elapsedS = ev.t ?? elapsedS
            if let p = ev.location { coordinate = p }
            walkedM = ev.s ?? walkedM
            vTargetKmh = ev.vTarget ?? vTargetKmh
            vActualKmh = ev.vActual ?? vActualKmh
            isLive = true
            return true

        case "progress":
            // ⚠️ 会话摘要也走这条路。`sigma_v` 只在实时指标里有，
            //    拿它当判别式（见文件头）。
            guard ev.sigmaV != nil else { return false }
            elapsedS = ev.t ?? elapsedS
            if let p = ev.location { coordinate = p }
            walkedM = ev.s ?? walkedM
            targetM = ev.targetM ?? targetM
            vTargetKmh = ev.vTarget ?? vTargetKmh
            vActualKmh = ev.vActual ?? vActualKmh
            sigmaV = ev.sigmaV ?? sigmaV
            sampleRate = ev.sampleRate ?? sampleRate
            isLive = true
            return true

        default:
            return false
        }
    }

    /// 会话结束后冻结读数 —— 保留最后一帧，让用户能核对停在哪儿。
    mutating func freeze() { isLive = false }

    /// 清空。下一次会话开始时必须调，否则会显示上一次的距离。
    mutating func reset() { self = LiveMetrics() }

    // MARK: - 派生量

    /// 剩余距离（米）。路线未知时给 0。
    var remainingM: Double { max(0, targetM - walkedM) }

    /// 完成度 0…1。路线未知时给 0。
    var fraction: Double {
        targetM > 0 ? min(1, max(0, walkedM / targetM)) : 0
    }

    /// 剩余时间的**朴素估计**：剩余距离 ÷ 当前实测速度。
    ///
    /// ⚠️ 这不是 ETA —— works.md §5.6 的 ETA 走闭式解，
    ///    由侧车的 `estimate` 提供（松手时才算一次）。
    ///    这里只是「按现在的速度跑，还要多久」的粗读数，
    ///    会随实际速度波动，两者**故意显示成不同格式**以示区别。
    var naiveRemainingS: Double {
        vActualKmh > 0.01 ? remainingM / (vActualKmh / 3.6) : 0
    }

    /// 当前配速（min/km）。速度为 0 时给 0。
    var paceMinPerKm: Double {
        vActualKmh > 0.01 ? 60.0 / vActualKmh : 0
    }

    // MARK: - 显示文案
    //
    //  全部放这里而不是散在视图里：文案是**契约的一部分**
    //  （用户要靠它读数），能单测就不该只靠肉眼。

    var walkedText: String { PreviewMath.distanceText(walkedM) }
    var targetText: String { PreviewMath.distanceText(targetM) }
    var elapsedText: String { PreviewMath.durationText(elapsedS) }

    var speedText: String {
        isLive ? String(format: "%.1f km/h", vActualKmh) : "—"
    }

    /// 目标速度（剖面给的），与实测分开显示 ——
    /// 两者出现差值正是这个工具要观察的现象。
    var targetSpeedText: String {
        isLive ? String(format: "%.1f km/h", vTargetKmh) : "—"
    }

    var sigmaText: String {
        isLive && sigmaV > 0 ? String(format: "±%.2f km/h", sigmaV) : "—"
    }

    var paceText: String {
        paceMinPerKm > 0 ? String(format: "%.2f min/km", paceMinPerKm) : "—"
    }

    /// 「0.85 / 3.00 km」—— 已走与全长并排。
    var progressText: String { "\(walkedText) / \(targetText)" }

    /// 剩下的朴素估计。**刻意不带「预计」二字**，与侧车闭式解的 ETA 区分。
    var naiveRemainingText: String {
        naiveRemainingS > 0 ? PreviewMath.durationText(naiveRemainingS) : "—"
    }
}
