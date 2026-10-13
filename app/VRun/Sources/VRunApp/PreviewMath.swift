import Foundation

// ══════════════════════════════════════════════════════════════════
//  PreviewMath.swift ★ —— §3.6 闭式解的 Swift 镜像
//
//  ⚠️ 纪律（works.md §11 / §5.6）
//     本文件**只能**实现 §3.6 的一阶近似。它**不是**第二真相源：
//     松手 250 ms 后一律以侧车 `estimate.result` 为准，两者差 > 1 s 时高亮提示。
//
//     它存在的唯一理由是**拖动时 60 fps 不等进程往返**。
//     `preview:estimateExact` 会把本地近似换成侧车的精确值。
//
//  数值口径与 `src/vrun/estimate.py` 完全一致，由 `PreviewMathTests`
//  对着 `tests/golden/estimate.json` 逐行比对 —— 两边都通过，
//  才说明「预览与实际一致」不是巧合。
// ══════════════════════════════════════════════════════════════════

/// §1.1 校园 App 的规则窗口。
enum Rules {
    static let vMin = 6.0        // R3 速度下限 km/h
    static let vMax = 17.0       // R4 速度上限 km/h
    static let dMinM = 3000.0    // R1 距离下限
    static let dMaxM = 15000.0   // R2 距离上限
    static let tMaxS = 7200.0    // R5 时长上限（2 h）

    /// 滑块的定义域 —— 端点就是 R3 / R4 的判定线本身（§5.2）。
    static let speedRange = vMin...vMax

    /// §3.2 默认剖面参数（与 Python 侧 SpeedProfile 一致）。
    static let tWarmup = 30.0     // 起步 ramp 时长 s
    static let startRatio = 0.8   // 起步初速比
    static let delta = -0.015     // 疲劳漂移
    static let finishBoost = 0.05 // 终点冲刺幅度
}

enum PreviewMath {

    // MARK: - §3.6 配速 / ETA

    /// 配速，秒每公里。`p = 3600 / v̄`
    static func pace(kmh: Double) -> Double { 3600.0 / kmh }

    /// 纯运动时间 `D / v`，秒。
    static func naiveEta(distanceM: Double, kmh: Double) -> Double {
        distanceM / (kmh / 3.6)
    }

    /// §3.6 一阶近似修正 ETA，秒。
    ///
    ///     T ≈ T_raw · (1 + |δ|/2 − 0.04·f) + t_w·(1 − r₀)/2
    ///
    /// 「为什么非算不可」—— 3 km 尺度下 9 秒就是 0.8%。
    /// 预览说 18:00 而实际 18:09，用户会以为引擎坏了。
    static func closedFormEta(distanceM: Double, kmh: Double) -> Double {
        let tRaw = naiveEta(distanceM: distanceM, kmh: kmh)
        return tRaw * (1.0 + abs(Rules.delta) / 2.0 - 0.04 * Rules.finishBoost)
             + Rules.tWarmup * (1.0 - Rules.startRatio) / 2.0
    }

    // MARK: - §3.5 表观速度抖动

    /// 朴素前后差分下的表观速度标准差 1σ，km/h。
    ///
    /// 反直觉但重要：**采样率越高，速度噪声越大**（∝ 1/√Δt）。
    /// 1 Hz 约 2.0 km/h，5 Hz 高达 4.7 —— 所以默认 1 Hz，不是越快越好。
    static func sigmaApparent(dt: Double, level: NoiseLevel) -> Double {
        guard dt > 0, level.scale > 0 else { return 0 }
        let dSlow = Self.disp(sigma: 3.0, tau: 300.0, dt: dt) * level.scale
        let dFast = Self.disp(sigma: 0.3, tau: 1.5, dt: dt) * level.scale
        let d = (2.0 * (dSlow * dSlow + dFast * dFast)).squareRoot()
        return (d / dt) * 3.6
    }

    /// 单轴一阶差分标准差 `dᵢ = σᵢ·√(2(1 − exp(−Δt/τᵢ)))`
    private static func disp(sigma: Double, tau: Double, dt: Double) -> Double {
        sigma * (2.0 * (1.0 - exp(-dt / tau))).squareRoot()
    }

    // MARK: - §5.3 安全带

    /// 某一既定速度下，瞬时速度的取值范围。
    ///
    /// ⚠️ 别和 `band` 搞混：
    /// - `band`    → **v̄ 的可行区间**，回答「滑块该停在哪儿」
    /// - `band` 之外 → **瞬时速度范围**，回答「现在这个设定会被噪声推到哪」
    static func velocitySpan(kmh: Double, sigmaV: Double) -> (lo: Double, hi: Double) {
        (kmh * (1 + Rules.delta) - 2 * sigmaV,
         kmh * (1 + Rules.finishBoost) + 2 * sigmaV)
    }

    /// 使 `levels·σ` 内**绝不越界**的 v̄ 区间。无解返回 `nil`。
    ///
    /// 严苛档（σ=3.0）时约束要求 `v̄ ≥ 12.18` 且 `v̄ ≤ 10.48`，交集为空 ——
    /// **这不是 bug，是真实结论**（works.md §5.3）。
    static func band(sigmaV: Double, levels: Double = 2) -> ClosedRange<Double>? {
        let lo = (Rules.vMin + levels * sigmaV) / (1 + Rules.delta)
        let hi = (Rules.vMax - levels * sigmaV) / (1 + Rules.finishBoost)
        guard lo <= hi else { return nil }
        return lo...hi
    }

    /// 三级判定（§5.3 的**规则表**，不是 §5.4 的示例列 —— 见 findings F2）。
    static func color(kmh: Double, sigmaV: Double) -> SafetyColor {
        if let b2 = band(sigmaV: sigmaV, levels: 2), b2.contains(kmh) { return .green }
        if let b1 = band(sigmaV: sigmaV, levels: 1), b1.contains(kmh) { return .yellow }
        return .red
    }

    // MARK: - §5.4 判定预期

    /// 按 R1–R5 给出 ✓ / ⚠️ / ✗。**只是预期，不是判决** —— 真实判定由校园 App 做出。
    static func verdict(distanceM: Double, kmh: Double, etaS: Double, sigmaV: Double) -> [String: String] {
        var v: [String: String] = [:]
        v["R1"] = distanceM >= Rules.dMinM ? "✓" : "✗ 不足 3 km"
        v["R2"] = distanceM <= Rules.dMaxM ? "✓" : "✗ 超过 15 km"
        v["R5"] = etaS <= Rules.tMaxS ? "✓" : "✗ 超过 2 h"

        if kmh < Rules.vMin {
            v["R3"] = "✗ 低于 6 km/h"
        } else if velocitySpan(kmh: kmh, sigmaV: sigmaV).lo < Rules.vMin {
            v["R3"] = "⚠️ 噪声下可能过慢"
        } else {
            v["R3"] = "✓"
        }

        if kmh > Rules.vMax {
            v["R4"] = "✗ 高于 17 km/h"
        } else if velocitySpan(kmh: kmh, sigmaV: sigmaV).hi > Rules.vMax {
            v["R4"] = "⚠️ 噪声下可能过快"
        } else {
            v["R4"] = "✓"
        }
        return v
    }

    /// 贴线设定（6.0 / 17.0）时**关闭夹紧** —— 贴线正是要测的情形（§3.2 末尾的 ⚠️）。
    /// 滑块不阻止越界设定，只负责如实预警。
    static func clampEnabled(kmh: Double) -> Bool {
        !(kmh <= Rules.vMin || kmh >= Rules.vMax)
    }

    // MARK: - §5.2 滑块映射
    //
    // 刻意做成**纯函数**而不是 SessionController 的实例方法：
    // 映射只依赖 Rules 与一个 Bool 开关，跟会话状态无关。
    // 好处是可在 XCTest 的非隔离上下文里直接验算（Swift 6 严格并发下
    // @MainActor 类型无法从同步测试方法构造），而且映射规则可被独立审阅。

    /// 滑块 0…1 的归一化位置（两种映射共用同一根滑条）。
    static func sliderFraction(for speed: Double, paceLinear: Bool) -> Double {
        let lo = Rules.speedRange.lowerBound, hi = Rules.speedRange.upperBound
        guard paceLinear else { return (speed - lo) / (hi - lo) }
        // 配速是反的：速度快 ⇒ 配秒数小
        let p = pace(kmh: speed)
        let pFast = pace(kmh: hi)   // 3:32
        let pSlow = pace(kmh: lo)   // 10:00
        return (pSlow - p) / (pSlow - pFast)
    }

    /// `sliderFraction` 的逆映射（带夹紧）。
    static func speedForFraction(_ f: Double, paceLinear: Bool) -> Double {
        let clamped = min(max(f, 0), 1)
        let lo = Rules.speedRange.lowerBound, hi = Rules.speedRange.upperBound
        guard paceLinear else { return lo + clamped * (hi - lo) }
        let pFast = pace(kmh: hi), pSlow = pace(kmh: lo)
        return 3600.0 / (pSlow - clamped * (pSlow - pFast))
    }

    // MARK: - 格式化

    static func paceText(_ sPerKm: Double) -> String {
        let total = Int(sPerKm.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }

    static func durationText(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let (h, m, s) = (total / 3600, (total % 3600) / 60, total % 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s)
                     : String(format: "%d:%02d", m, s)
    }

    static func distanceText(_ meters: Double) -> String {
        meters >= 1000 ? String(format: "%.2f km", meters / 1000)
                       : String(format: "%.0f m", meters)
    }
}
