import SwiftUI

// ══════════════════════════════════════════════════════════════════
//  SafetyBand.swift ★ —— §5.3 噪声安全带色带
//
//  这是**面板最有价值的部分**：把 §3.5 那个反直觉结论变成看得见的东西。
//
//  §3.5 的结论：合法窗口是 6–17 km/h，但真正「怎么抖都不会被误判」的
//  只有 **10.2–12.4**。噪声足以吃掉半个合法窗口。
//
//  ⚠️ 严苛档（σ=3.0）时 2σ 安全带**交集为空** —— 这不是 bug，是真实结论。
//     此时**不画绿带**，直接显示原因。work.md §5.3 明写：
//     「如实画出来，比假装有安全带更有价值」。
// ══════════════════════════════════════════════════════════════════

struct SafetyBand: View {
    let speed: Double
    let sigmaV: Double
    let band1: ClosedRange<Double>?
    let band2: ClosedRange<Double>?
    let color: SafetyColor
    /// 滑块是否被锁（运行中只读）
    let locked: Bool

    private let lo = Rules.speedRange.lowerBound
    private let hi = Rules.speedRange.upperBound

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            track
            legend
            if band2 == nil { degenerateWarning }
        }
    }

    // MARK: 标题行

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "shield.lefthalf.filled")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color.safety(color))
            Text("噪声安全带").font(.system(size: 11, weight: .semibold))
            Spacer()
            Text("σ = \(String(format: "%.2f", sigmaV)) km/h")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
        }
    }

    // MARK: 色带本体

    private var track: some View {
        GeometryReader { geo in
            let w = geo.size.width
            ZStack(alignment: .leading) {
                // 底：规则窗口 6–17（合法但未必安全）
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color.secondary.opacity(0.10))

                // 1σ 黄带先铺（更宽），2σ 绿带后盖（更窄）
                if let b1 = band1 {
                    let r = rect(for: b1, width: w)
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Color.warnYellow.opacity(0.28))
                        .frame(width: r.width)
                        .offset(x: r.minX)
                }
                if let b2 = band2 {
                    let r = rect(for: b2, width: w)
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Color.safeGreen.opacity(0.60))
                        .frame(width: r.width)
                        .offset(x: r.minX)
                }

                cursor(width: w)
            }
            .frame(height: 22)
            .overlay(alignment: .leading) {
                HStack {
                    Text(String(format: "%.0f", lo)).font(.system(size: 8, design: .monospaced))
                    Spacer()
                    Text(String(format: "%.0f", hi)).font(.system(size: 8, design: .monospaced))
                }
                .foregroundStyle(.tertiary)
                .offset(y: 13)
            }
            .padding(.bottom, 14)
        }
        .frame(height: 36)
    }

    private func frac(_ v: Double) -> CGFloat {
        CGFloat((min(max(v, lo), hi) - lo) / (hi - lo))
    }

    /// 把「速度区间」映射成色带在该轨道上的矩形。
    ///
    /// ⚠️ 刻意**返回矩形而不是形状** —— 中间隔了一层 `.frame()` / `.offset()`
    /// 之后返回类型已被擦成 `some View`，调用方就再也 `.fill()` 不上了。
    private func rect(for b: ClosedRange<Double>, width: CGFloat) -> CGRect {
        let x = frac(b.lowerBound) * width
        let w = max(frac(b.upperBound) * width - x, 1)
        return CGRect(x: x, y: 0, width: w, height: 22)
    }

    /// 当前速度的游标：一根竖线 + 一个小三角。
    private func cursor(width: CGFloat) -> some View {
        let x = frac(speed) * width
        return ZStack(alignment: .top) {
            Rectangle()
                .fill(.primary)
                .frame(width: 2, height: 22)
                .offset(x: x - 1)
            Image(systemName: "arrowtriangle.down.fill")
                .font(.system(size: 7))
                .foregroundStyle(.primary)
                .offset(x: x - 5, y: -8)
        }
    }

    // MARK: 图例

    private var legend: some View {
        HStack(spacing: 10) {
            LegendDot(color: .riskRed, text: "过慢")
            LegendDot(color: .safeGreen, text: "2σ 安全")
            LegendDot(color: .warnYellow, text: "1σ 安全")
            LegendDot(color: .riskRed, text: "过快")
            Spacer()
        }
    }

    // MARK: 严苛档退化提示

    /// 严苛档无 2σ 安全带时的说明。**这是真实结论，不是错误。**
    private var degenerateWarning: some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10))
                .foregroundStyle(Color.warnYellow)
            Text("严苛噪声下无 2σ 安全带（σ = \(String(format: "%.1f", sigmaV)) km/h 已达规则窗口的 \(Int((sigmaV * 2 / (hi - lo) * 100).rounded()))%）——无论设什么速度都无法保证不越界。")
                .font(.system(size: 9))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 2)
    }
}

private struct LegendDot: View {
    let color: Color
    let text: String
    var body: some View {
        HStack(spacing: 3) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(text).font(.system(size: 9)).foregroundStyle(.secondary)
        }
    }
}
