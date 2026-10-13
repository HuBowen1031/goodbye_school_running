import SwiftUI

// ══════════════════════════════════════════════════════════════════
//  Style.swift —— Liquid Glass 设计令牌
//
//  macOS 26+ 的 Liquid Glass 有一条关键规则：
//  **相邻的玻璃元件会自动融合**，但前提是它们在同一个 `GlassEffectContainer` 里。
//  不放进容器的话，每个元件各自折射，边界会出现难看的接缝。
//
//  所以这里的 `GlassPanel` / `Chip` 是玻璃外壳的**统一入口** ——
//  不要在别处直接写 `.glassEffect(...)`，统一入口才能保证风格一致。
//
//  ⚠️ 注意：SDK 里的类型就叫 `Glass`，**不要自己再定义一个同名枚举**
//     —— 会把它遮蔽掉，且 `.regular` / `.identity` 这些静态成员会全部失联。
// ══════════════════════════════════════════════════════════════════

extension Color {
    /// 安全带三色（works.md §5.3）。语义色，不是装饰色。
    static let safeGreen  = Color(red: 0.20, green: 0.78, blue: 0.45)
    static let warnYellow = Color(red: 0.98, green: 0.74, blue: 0.20)
    static let riskRed    = Color(red: 0.95, green: 0.36, blue: 0.36)

    static func safety(_ c: SafetyColor) -> Color {
        switch c {
        case .green:  return .safeGreen
        case .yellow: return .warnYellow
        case .red:    return .riskRed
        }
    }

    /// 日志等级配色（草图：日志面板「尽量彩色」）。
    static func level(_ l: LogLevel) -> Color {
        switch l {
        case .info:    return .accentColor
        case .success: return .safeGreen
        case .warn:    return .warnYellow
        case .error:   return .riskRed
        case .debug:   return .secondary
        }
    }
}

/// 组装 `SwiftUI.Glass`。用函数而不是再定义一个类型 —— 避免遮蔽 SDK 的 `Glass`。
func glassSpec(_ tint: Color? = nil, interactive: Bool = false) -> Glass {
    var g: Glass = .regular
    if let tint { g = g.tint(tint) }
    if interactive { g = g.interactive() }
    return g
}

// ══════════════════════════════════════════════════════════════════
//  可复用的玻璃容器
// ══════════════════════════════════════════════════════════════════

/// 玻璃面板。Liquid Glass 自带折射层，这里只补一圈边缘高光描边。
struct GlassPanel<Content: View>: View {
    var cornerRadius: CGFloat = 18
    var padding: CGFloat = 14
    var tint: Color?
    var interactive: Bool = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(glassSpec(tint, interactive: interactive),
                         in: .rect(cornerRadius: cornerRadius, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(
                        LinearGradient(colors: [.white.opacity(0.45), .white.opacity(0.06)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing),
                        lineWidth: 0.8)
            }
    }
}

/// 标题行：图标 + 文字 + 可选尾部内容。
struct PanelHeader<Trailing: View>: View {
    let icon: String
    let title: String
    var subtitle: String?
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 0) {
                Text(title).font(.system(size: 12, weight: .semibold))
                if let subtitle {
                    Text(subtitle).font(.system(size: 9))
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 4)
            trailing()
        }
    }
}

extension PanelHeader where Trailing == EmptyView {
    init(icon: String, title: String, subtitle: String? = nil) {
        self.init(icon: icon, title: title, subtitle: subtitle) { EmptyView() }
    }
}

/// 小圆角标签（预设档位、吸附点、噪声档）。
struct Chip: View {
    let text: String
    var active: Bool = false
    var tint: Color = .accentColor
    var action: (() -> Void)?

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: active ? .semibold : .regular, design: .rounded))
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background {
                Capsule().fill(active ? tint.opacity(0.9) : Color.secondary.opacity(0.12))
            }
            .foregroundStyle(active ? Color.white : Color.secondary)
            .contentShape(Capsule())
            .onTapGesture { action?() }
            .glassEffect(active ? Glass.identity
                                 : Glass.regular.tint(Color.secondary.opacity(0.06)),
                         in: .capsule)
    }
}

/// 键值对的一行（面板里到处都要用）。
struct KV: View {
    let k: String
    let v: String
    var color: Color = .primary
    var mono: Bool = true
    var note: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(k).font(.system(size: 11)).foregroundStyle(.secondary)
            Spacer(minLength: 6)
            Text(v)
                .font(.system(size: 11, weight: .medium,
                              design: mono ? .monospaced : .default))
                .foregroundStyle(color)
            if let note {
                Text(note).font(.system(size: 9)).foregroundStyle(.tertiary)
            }
        }
    }
}
