import SwiftUI

// ══════════════════════════════════════════════════════════════════
//  LogPanel.swift —— 可滚动的彩色日志面板
//
//  草图要求：
//    「可滚动的，尽量彩色」
//    「输出的日志（注入状态，IPC 日志）」
//    「→ 做日检查要性过滤功能」   ← 按天筛选
//
//  设计取舍
//    日志量最大的场景是 1 Hz 的 tick —— 每秒一条会让有用信息被淹没。
//    所以默认**隐藏 debug 级**，只留 info 以上；debug 可一键展开。
// ══════════════════════════════════════════════════════════════════

struct LogPanel: View {
    @ObservedObject var store: LogStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            dayFilter
            Divider().opacity(0.35)
            if store.visible.isEmpty {
                empty
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(store.visible) { entry in
                                row(entry).id(entry.id)
                            }
                        }
                        .padding(.trailing, 4)
                    }
                    .onChange(of: store.visible.count) { _, _ in
                        if store.autoScroll, let last = store.visible.last {
                            withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                        }
                    }
                }
            }
        }
    }

    // MARK: 头

    private var header: some View {
        PanelHeader(icon: "terminal", title: "运行日志",
                    subtitle: "注入状态 · IPC 通道") {
            HStack(spacing: 4) {
                Image(systemName: store.autoScroll ? "arrow.down.circle.fill" : "arrow.down.circle")
                    .font(.system(size: 10))
                    .foregroundStyle(store.autoScroll ? Color.accentColor : Color.secondary)
                    .onTapGesture { store.autoScroll.toggle() }
                Image(systemName: store.showDebug ? "eye.fill" : "eye.slash")
                    .font(.system(size: 10))
                    .foregroundStyle(store.showDebug ? Color.accentColor : Color.secondary)
                    .onTapGesture { store.showDebug.toggle() }
                Image(systemName: "trash")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .onTapGesture { store.clear() }
            }
        }
    }

    // MARK: 按天筛选

    private var dayFilter: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 4) {
                Chip(text: "全部", active: store.dayFilter == nil) { store.dayFilter = nil }
                ForEach(store.days, id: \.self) { d in
                    Chip(text: store.shortDay(d),
                         active: store.dayFilter == d,
                         tint: .warnYellow) {
                        store.dayFilter = (store.dayFilter == d) ? nil : d
                    }
                }
            }
        }
        .frame(height: 18)
    }

    // MARK: 单行

    private func row(_ e: LogEntry) -> some View {
        HStack(alignment: .top, spacing: 5) {
            Text(Self.clock.string(from: e.date))
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.quaternary)
            Text(e.level.symbol)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(Color.level(e.level))
                .frame(width: 9)
            Text(e.text)
                .font(.system(size: 10))
                .foregroundStyle(e.level == .debug ? .secondary : .primary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    private var empty: some View {
        Text(store.dayFilter == nil ? "暂无日志" : "这一天没有日志")
            .font(.system(size: 10))
            .foregroundStyle(.tertiary)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 14)
    }

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}

// ══════════════════════════════════════════════════════════════════
//  LogStore —— 日志的持有者
// ══════════════════════════════════════════════════════════════════

@MainActor
final class LogStore: ObservableObject {

    @Published private(set) var entries: [LogEntry] = []
    @Published var dayFilter: String?      // nil = 全部
    @Published var showDebug = false
    @Published var autoScroll = true

    /// 上限。1 Hz 的 tick 会让日志爆炸，必须有天花板。
    private let cap = 4000

    func append(_ e: LogEntry) {
        entries.append(e)
        if entries.count > cap { entries.removeFirst(entries.count - cap) }
    }

    func clear() { entries.removeAll() }

    /// 有哪些天（倒序，最新在前）
    var days: [String] {
        var seen = Set<String>()
        return entries.reversed().compactMap { seen.insert($0.day).inserted ? $0.day : nil }
    }

    var visible: [LogEntry] {
        entries.filter { e in
            (showDebug || e.level != .debug) &&
            (dayFilter == nil || e.day == dayFilter!)
        }
    }

    /// 「今天」/「10-12」这种短标签，省地方
    func shortDay(_ d: String) -> String {
        let cal = Calendar.current
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.locale = Locale(identifier: "en_US_POSIX")
        guard let date = f.date(from: d) else { return d }
        if cal.isDateInToday(date) { return "今天" }
        if cal.isDateInYesterday(date) { return "昨天" }
        let g = DateFormatter(); g.dateFormat = "MM-dd"
        return g.string(from: date)
    }
}
