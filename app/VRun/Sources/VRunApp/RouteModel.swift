import Foundation
import CoreLocation

// ══════════════════════════════════════════════════════════════════
//  RouteModel.swift —— 路线点集与编辑
//
//  为什么单独抽出来（file-manifest §5.1）
//     若把编辑逻辑塞进 RouteCanvas.swift，视图与数据会互相纠缠，
//     撤销几乎写不对。本文件**不 import SwiftUI 视图**，
//     因此可被单测直接驱动。
//
//  钢笔工具的语义（草图）
//     「类似『钢笔』工具，不要求用户一笔画完，只要从起点开始连续画即可」
//     → 点一下加一个点；**允许中途停笔再续**；
//       Esc 或「结束」收笔；⌘Z 撤销最后一个点。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class RouteModel: ObservableObject {

    @Published private(set) var points: [GeoPoint] = []
    /// 总长度（米）。每次增删点重算。
    @Published private(set) var totalLengthM: Double = 0
    /// 撤销栈：保存被删掉的点，重做不做（这个工具不需要）。
    private var undoStack: [GeoPoint] = []

    /// 钢笔是否处于「正在画」状态。
    @Published private(set) var isDrawing = false

    /// 设备上报的位置（= 上次注入点）。由 SessionController 从 `device` 事件写入。
    @Published var deviceLocation: GeoPoint?

    /// 设备是否已连接。**绿点的取值依赖它**，见 :func:`startAnchor(deviceConnected:)`。
    @Published var deviceConnected: Bool = false

    // MARK: - 起点标记（绿圈）

    /// ★ 绿圈画在哪 —— 一条**纯函数**，两种情形共用同一个视觉。
    ///
    /// 2026-10-14 用户指出（`user_found_problem.md` 第 1 条）：
    /// 原规格是「以 iPhone 当前真实位置作为画图起点」，但设备没连时绿圈不会出现，
    /// 用户却仍被要求「从这里起笔」——**根本没法开始画**。
    ///
    /// 现在统一成「绿圈 = 路线起点」：
    ///
    /// | 情形 | 绿圈位置 | 用户看到的提示 |
    /// | --- | --- | --- |
    /// | 设备已连接且报过位置 | 设备位置 | 绿：「从 iPhone 当前位置起笔」 |
    /// | 设备未连接 / 位置未知 | **路线的第一个点** | 红：「未检测到 iPhone，点一下地图作为起点」 |
    /// | 两者都没有 | 无（画布为空） | 红：「未检测到 iPhone，点一下地图作为起点」 |
    ///
    /// ★ 刻意不写成「设备没连就不画绿点」——
    ///     那样用户点击之后没有任何锚点反馈，
    ///     「我点的这个点到底算不算起点」会变得不确定。
    ///     把第一个点标绿，等于用视觉直接回答了这个问题。
    ///
    /// - Parameter deviceConnected: 设备当前是否已连接。
    /// - Returns: 绿圈应处的位置；`nil` 表示此刻不该画。
    func startAnchor(deviceConnected: Bool) -> GeoPoint? {
        if deviceConnected, let dev = deviceLocation { return dev }
        return points.first
    }

    // MARK: - 增删

    /// 加一个点。**钢笔的核心**：从起点连续加，不必一笔画完。
    func add(_ p: GeoPoint) {
        points.append(p)
        isDrawing = true
        recompute()
    }

    func undo() {
        guard let last = points.popLast() else { return }
        undoStack.append(last)
        isDrawing = !points.isEmpty
        recompute()
    }

    func clear() {
        points.removeAll()
        undoStack.removeAll()
        totalLengthM = 0
        isDrawing = false
    }

    func finish() { isDrawing = false }

    // MARK: - 缩放

    /// 按目标距离等比缩放（§5.5 的「目标距离」入口）。
    ///
    /// **锚在起点（第一个点）**，与 Python 侧 `Route.scaled_to` 同构 ——
    /// 地图上随手画的路线只提供「赛道形状」，长度由面板决定。
    ///
    /// ★ 为什么锚在起点而不是质心
    ///   绿圈的语义是「路线起点」（`startAnchor`），
    ///   而侧车注入的也是 `route.point_at(0)`（`session.prepare`）。
    ///   若绕质心缩放，起点会被推走：画一个 30 m 直径的圈再缩放到 3000 m，
    ///   起点会**漂 451 m**（实测）——而界面**不报任何错**。
    ///   锚在起点则符合直觉：起点是你选的，形状从起点长出去。
    @discardableResult
    func scaled(to targetM: Double) -> Bool {
        guard points.count >= 2, totalLengthM > 0, targetM > 0 else { return false }
        let factor = targetM / totalLengthM
        // ★ 锚点 = 第一个点（起点），不是质心。
        let aLat = points[0].lat
        let aLon = points[0].lon
        points = points.map { GeoPoint(lat: aLat + ($0.lat - aLat) * factor,
                                       lon: aLon + ($0.lon - aLon) * factor) }
        recompute()
        return true
    }

    // MARK: - 度量

    /// 经纬度 → 局部 ENU 平面（米），与 Python 侧 `ENUProjector` 同构。
    ///
    /// 成都一带经度方向必须乘 `cos(lat)`，漏乘会让东西向距离高估 16%。
    private static let earthRadiusM = 6_371_008.8
    private static let mPerDegLat = Double.pi * earthRadiusM / 180.0

    static func haversine(_ a: GeoPoint, _ b: GeoPoint) -> Double {
        let r = earthRadiusM
        let p1 = a.lat * .pi / 180, p2 = b.lat * .pi / 180
        let dp = p2 - p1
        let dl = (b.lon - a.lon) * .pi / 180
        let s = sin(dp / 2) * sin(dp / 2) + cos(p1) * cos(p2) * sin(dl / 2) * sin(dl / 2)
        return 2 * r * atan2(sqrt(s), sqrt(1 - s))
    }

    private func recompute() {
        guard points.count >= 2 else { totalLengthM = 0; return }
        var sum = 0.0
        for i in 1..<points.count { sum += Self.haversine(points[i - 1], points[i]) }
        totalLengthM = sum
    }

    /// 是否可以开始一次注入：至少两个点且够长。
    var isRoutable: Bool { points.count >= 2 && totalLengthM > 10 }
}
