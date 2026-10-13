import SwiftUI

// ══════════════════════════════════════════════════════════════════
//  SpeedPanel.swift ★ —— §5.1 参数面板
//
//  用户 90% 的时间只看这一块。布局严格照 works.md §5.1。
//
//  ★ 滑块的两个刻意的选择
//   ① 定义域 [6.0, 17.0] —— **端点就是规则 R3/R4 的判定线**，
//      不是随手取的数值。端点是「贴线测试」的天然入口。
//   ② 默认**配速线性**映射 —— 跑步者的心智单位是配速不是 km/h。
//      但面板**同时双显** km/h 与 min/km，因为规则原文是 km/h 表述，
//      对齐判定时必须用 km/h。（§5.2）
// ══════════════════════════════════════════════════════════════════

struct SpeedPanel: View {
    @ObservedObject var session: SessionController
    var onDebounceCommit: () -> Void

    private let presets: [(String, Double)] = [
        ("7", 7.0), ("8.5", 8.5), ("10", 10.0), ("12", 12.0),
        ("14", 14.0), ("16.5", 16.5),
    ]

    var body: some View {
        let p = session.preview()

        VStack(alignment: .leading, spacing: 10) {
            header(p)
            slider(p)
            detents
            SafetyBand(speed: session.speedKmh, sigmaV: p.sigmaV,
                        band1: p.band1, band2: p.band2,
                        color: p.color, locked: session.isSpeedLocked)
            pickers
            Divider().opacity(0.4)
            readouts(p)
            verdict(p)
            driftWarning
        }
    }

    // MARK: 标题

    private func header(_ p: SessionController.Preview) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(String(format: "%.1f", session.speedKmh))
                .font(.system(size: 26, weight: .bold, design: .rounded))
                .contentTransition(.numericText())
                .foregroundStyle(Color.safety(p.color))
            Text("km/h").font(.system(size: 11)).foregroundStyle(.secondary)
            Spacer()
            VStack(alignment: .trailing, spacing: 0) {
                Text("配速 \(p.paceStr)").font(.system(size: 13, weight: .medium, design: .monospaced))
                Text("窗口 \(Int(Rules.vMin))–\(Int(Rules.vMax)) km/h")
                    .font(.system(size: 9)).foregroundStyle(.tertiary)
            }
        }
    }

    // MARK: 滑块

    private func slider(_ p: SessionController.Preview) -> some View {
        VStack(spacing: 2) {
            Slider(
                value: Binding(
                    get: { session.sliderFraction(for: session.speedKmh) },
                    set: { session.speedKmh = session.speedForFraction($0) }
                ),
                in: 0...1
            )
            .disabled(session.isSpeedLocked)     // §5.7 运行中锁只读
            .controlSize(.small)
            .tint(Color.safety(p.color))
            .glassEffect(Glass.regular.tint(Color.safety(p.color).opacity(0.35)),
                         in: .capsule)

            HStack {
                Text(session.paceLinear ? "10:00 /km" : "6.0 km/h")
                Spacer()
                Text(session.paceLinear ? "3:32 /km" : "17.0 km/h")
            }
            .font(.system(size: 8, design: .monospaced))
            .foregroundStyle(.tertiary)
        }
    }

    // MARK: 吸附点

    private var detents: some View {
        HStack(spacing: 5) {
            ForEach(presets, id: \.1) { label, v in
                Chip(text: label,
                     active: abs(session.speedKmh - v) < 0.05,
                     tint: Color.safety(session.preview().color)) {
                    guard !session.isSpeedLocked else { return }
                    session.speedKmh = v
                    onDebounceCommit()
                }
            }
            Chip(text: session.paceLinear ? "配速轴" : "速度轴",
                 active: true, tint: .secondary) {
                session.paceLinear.toggle()
            }
            Spacer()
        }
    }

    // MARK: 距离 / 采样率 / 噪声

    private var pickers: some View {
        VStack(spacing: 6) {
            HStack(spacing: 5) {
                Text("距离").font(.system(size: 10)).foregroundStyle(.secondary)
                ForEach([3000.0, 5000.0, 10000.0, 15000.0], id: \.self) { d in
                    Chip(text: String(format: "%.0f", d / 1000),
                         active: abs(session.targetDistanceM - d) < 1) {
                        session.targetDistanceM = d
                        onDebounceCommit()
                    }
                }
                TextField("自定义", value: $session.targetDistanceM, format: .number)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 10, design: .monospaced))
                    .frame(width: 66)
                    .onSubmit { onDebounceCommit() }
                Spacer()
            }

            HStack(spacing: 5) {
                Text("采样率").font(.system(size: 10)).foregroundStyle(.secondary)
                // ★ 默认 1 Hz —— §3.5：采样率越高，朴素差分的速度噪声越大
                ForEach([5.0, 2.0, 1.0, 0.5], id: \.self) { r in
                    Chip(text: r == 1 ? "1 Hz" : String(format: "%g", r),
                         active: abs(session.sampleRate - r) < 0.01) {
                        session.sampleRate = r
                        onDebounceCommit()
                    }
                }
                Spacer()
                Text("非越高越好").font(.system(size: 8)).foregroundStyle(.tertiary)
            }

            HStack(spacing: 5) {
                Text("噪声").font(.system(size: 10)).foregroundStyle(.secondary)
                ForEach(NoiseLevel.allCases) { n in
                    Chip(text: n.label, active: session.noise == n) {
                        session.noise = n
                        onDebounceCommit()
                    }
                }
                Spacer()
            }
        }
    }

    // MARK: 读数

    private func readouts(_ p: SessionController.Preview) -> some View {
        VStack(spacing: 3) {
            KV(k: "预计用时", v: p.etaStr, color: .primary,
               note: "含起步/漂移/冲刺")
            KV(k: "纯运动", v: p.naiveStr, color: .secondary)
            KV(k: "真实占用", v: p.etaStr, color: Color.warnYellow, note: "无法加速")
            KV(k: "表观 1σ", v: String(format: "±%.2f km/h", p.sigmaV), color: .secondary)
            KV(k: "2σ 表观区间",
               v: String(format: "[%.1f, %.1f]", p.span.lo, p.span.hi),
               color: p.span.lo < Rules.vMin || p.span.hi > Rules.vMax ? .riskRed : .secondary)
        }
    }

    // MARK: R1–R5 判定预期

    private func verdict(_ p: SessionController.Preview) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("判定预期").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            HStack(spacing: 6) {
                ForEach(["R1", "R3", "R4", "R5"], id: \.self) { k in
                    let v = p.verdict[k] ?? ""
                    HStack(spacing: 3) {
                        Text(k).font(.system(size: 9, weight: .semibold, design: .monospaced))
                        Text(v.hasPrefix("✓") ? "✓" : (v.hasPrefix("✗") ? "✗" : "⚠"))
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(v.hasPrefix("✓") ? Color.safeGreen
                                            : (v.hasPrefix("✗") ? Color.riskRed : Color.warnYellow))
                    }
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(Capsule().fill(Color.secondary.opacity(0.10)))
                }
                Spacer()
            }
            Text("仅供实测比对，真实判定由校园 App 做出")
                .font(.system(size: 8)).foregroundStyle(.tertiary)
        }
    }

    // MARK: 预览漂移提示

    /// ★ §5.6：本地近似与侧车精确值差 > 1 s 时高亮。
    /// 这不只是防错 —— 它把「预览是否可信」变成用户每次操作都会自动验证的事。
    @ViewBuilder
    private var driftWarning: some View {
        if let e = session.exact, session.driftSeconds > 1.0 {
            HStack(spacing: 5) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 9)).foregroundStyle(Color.warnYellow)
                Text(String(format: "预览与侧车精确值相差 %.1f s（本次参数下正常）", session.driftSeconds))
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
            }
        } else if session.exact != nil {
            HStack(spacing: 5) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 9)).foregroundStyle(Color.safeGreen)
                Text("预览与侧车一致")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                Spacer()
            }
        }
    }
}
