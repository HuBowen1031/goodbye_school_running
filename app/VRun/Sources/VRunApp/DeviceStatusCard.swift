import SwiftUI

// ══════════════════════════════════════════════════════════════════
//  DeviceStatusCard.swift —— 侧栏顶部的设备状态卡（用户新增需求）
//
//  来自 `user_found_problem.md` 第 2 条：
//    「如果没有插上手机，是不是该让提示变红，让用户插入手机然后
//      点击一个按钮刷新状态？这个按钮可以放在侧栏，
//      没有连接手机的时候，用强调色或者别的办法提示用户按它刷新」
//
//  ★ 设计上的一个关键区分：**「还没问过」≠「没连上」**
//     设备是热插拔的。GUI 刚起来时如果用户还没插手机，
//     我们无从得知 —— 所以在用户点过刷新之前，提示语是
//     「尚未检测」而不是「未检测到设备」。
//     写成后者会让人以为设备真的不在，而事实是我们**没问过**。
//     这一条在 `SessionController.hasQueriedDevice` 上。
// ══════════════════════════════════════════════════════════════════

struct DeviceStatusCard: View {

    @ObservedObject var session: SessionController

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            header
            detail
            Spacer(minLength: 0)
        }
    }

    // MARK: 标题行 + 刷新按钮

    private var header: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(stateColor)
                .frame(width: 7, height: 7)
                .overlay(Circle().stroke(stateColor.opacity(0.35), lineWidth: 3))
            Text("iPhone").font(.system(size: 11, weight: .semibold))
            Spacer(minLength: 4)
            refreshButton
        }
    }

    /// 刷新按钮。
    ///
    /// ★ 断开时用**强调色填充**而不是只换个图标 ——
    ///   「设备没连」是一个需要用户行动的状态（去插线），
    ///   而非仅供阅读的信息。安静的描边按钮在这种场合会被忽略。
    private var refreshButton: some View {
        Button {
            session.refreshDeviceStatus()
            log(.info, "正在刷新设备状态…")
        } label: {
            HStack(spacing: 4) {
                Image(systemName: session.isRefreshingDevice ? "hourglass" : "arrow.clockwise")
                    .font(.system(size: 9, weight: .semibold))
                Text(session.isRefreshingDevice ? "检测中" : "刷新")
                    .font(.system(size: 10, weight: .medium))
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(session.deviceConnected ? Color.primary : Color.white)
            .background {
                if session.deviceConnected {
                    Capsule().fill(Color.secondary.opacity(0.14))
                } else {
                    Capsule().fill(Color.riskRed)
                }
            }
        }
        .buttonStyle(.plain)
        // 正在探测时禁用，防止连点把 RSD 隧道（进程级单例）打成并发
        .disabled(session.isRefreshingDevice)
        .help("向侧车问一次 iPhone 的连接状态")
        .accessibilityLabel("刷新设备状态")
    }

    // MARK: 明细

    @ViewBuilder
    private var detail: some View {
        if !session.hasQueriedDevice {
            hint("尚未检测。插入手机后点「刷新」。", color: .secondary)
        } else if session.deviceConnected {
            VStack(alignment: .leading, spacing: 2) {
                Text(session.deviceName ?? "已连接")
                    .font(.system(size: 10, weight: .medium)).foregroundStyle(.primary)
                if session.route.deviceLocation != nil {
                    Text("绿圈 = iPhone 当前上报位置")
                        .font(.system(size: 9)).foregroundStyle(.secondary)
                } else {
                    // 连上了但还没注入过 —— 绿圈现在画在用户自己点的第一个点上。
                    // 不说的话，用户会困惑「都说连上了，怎么绿圈还在我自己点的地方」。
                    Text("已连接，但尚无注入位置 —— 绿圈将从你点下的第一个点起")
                        .font(.system(size: 9)).foregroundStyle(Color.warnYellow)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        } else {
            VStack(alignment: .leading, spacing: 2) {
                hint("未检测到 iPhone", color: .riskRed)
                Text(session.deviceError ?? "请插入并解锁手机，点「刷新」重试")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func hint(_ text: String, color: Color) -> some View {
        Text(text).font(.system(size: 10, weight: .medium)).foregroundStyle(color)
    }

    /// 绿 = 已连接；红 = 问过了、确实没连；灰 = 还没问过。
    private var stateColor: Color {
        if session.deviceConnected { return .safeGreen }
        return session.hasQueriedDevice ? .riskRed : .secondary
    }

    private func log(_ level: LogLevel, _ text: String) {
        session.logSink?(.now(level, text))
    }
}