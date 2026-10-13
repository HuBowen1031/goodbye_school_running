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

    /// iPhone 当前定位 —— 草图里那个绿圈的来源。
    @Published var deviceLocation: GeoPoint?

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
    /// 绕质心缩放，与 Python 侧 `Route.scaled_to` 同构 ——
    /// 地图上随手画的路线只提供「赛道形状」，长度由面板决定。
    @discardableResult
    func scaled(to targetM: Double) -> Bool {
        guard points.count >= 2, totalLengthM > 0, targetM > 0 else { return false }
        let n = Double(points.count)
        let cLat = points.reduce(0) { $0 + $1.lat } / n
        let cLon = points.reduce(0) { $0 + $1.lon } / n
        let factor = targetM / totalLengthM
        points = points.map { GeoPoint(lat: cLat + ($0.lat - cLat) * factor,
                                       lon: cLon + ($0.lon - cLon) * factor) }
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
