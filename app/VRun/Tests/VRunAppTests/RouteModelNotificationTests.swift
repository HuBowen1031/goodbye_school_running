import XCTest
@testable import VRunApp

// ══════════════════════════════════════════════════════════════════
//  RouteModelNotificationTests —— @Published 是否真的发了通知
//
//  ★ 2026-10-14 实测背景
//
//     现象：点地图后 `points.count` 确实变了（日志可证），但界面
//     **只有第一次**会重绘，之后 HUD 永远停在旧值。
//
//     元凶是 Swift 的 `@Published` + `mutating` 方法组合：
//     `@Published var points: [GeoPoint] = []` 上调 `points.append(p)`，
//     Swift 会优先走 property wrapper 的 `_modify` 访问器，
//     它**绕过 `willSet`** ⇒ `objectWillChange` 不发 ⇒ SwiftUI 不重绘。
//
//     ⚠️ 为什么它「看起来时灵时不灵」：
//        第一次之所以生效，多半是被**别的**状态变化（estimate 的
//        250 ms 去抖回填、`device` 事件里的 `deviceConnected` 赋值…）
//        顺带重绘了。等那些变化不再发生，就再也没有人重绘。
//        ⇒ 症状与「地图不响应」「绿圈不出现」几乎无法区分。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class RouteModelNotificationTests: XCTestCase {

    /// ★ 核心断言：`add` **必须**发出 `objectWillChange`。
    func testAddPublishesObjectWillChange() {
        let m = RouteModel()
        var notifications = 0
        let token = m.objectWillChange.sink { _ in notifications += 1 }
        defer { token.cancel() }

        m.add(GeoPoint(lat: 30.5, lon: 104.0))

        XCTAssertGreaterThan(notifications, 0,
                             "add() 没发 objectWillChange —— SwiftUI 不会重绘，"
                             + "界面会永远停在旧值")
        XCTAssertEqual(m.points.count, 1)
    }

    /// 连续多次 add 也要**每次**都发，不能只有第一次。
    func testEveryAddPublishesNotJustTheFirst() {
        let m = RouteModel()
        var notifications = 0
        let token = m.objectWillChange.sink { _ in notifications += 1 }
        defer { token.cancel() }

        for i in 0..<5 {
            m.add(GeoPoint(lat: 30.5 + Double(i) * 0.001, lon: 104.0))
        }

        XCTAssertTrue(notifications >= 5,
                              "5 次 add 只发了 \(notifications) 次通知 —— "
                              + "后续几次界面不会刷新")
    }

    /// `undo` 同样必须发通知，否则「撤销」在界面上没反应。
    func testUndoPublishesObjectWillChange() {
        let m = RouteModel()
        m.add(GeoPoint(lat: 30.5, lon: 104.0))
        m.add(GeoPoint(lat: 30.6, lon: 104.1))

        var notifications = 0
        let token = m.objectWillChange.sink { _ in notifications += 1 }
        defer { token.cancel() }

        m.undo()
        XCTAssertGreaterThan(notifications, 0, "undo() 没发通知")
        XCTAssertEqual(m.points.count, 1)
    }

    /// `clear` 同理 —— 「清空路线」按钮必须让界面立刻反映出来。
    func testClearPublishesObjectWillChange() {
        let m = RouteModel()
        m.add(GeoPoint(lat: 30.5, lon: 104.0))

        var notifications = 0
        let token = m.objectWillChange.sink { _ in notifications += 1 }
        defer { token.cancel() }

        m.clear()
        XCTAssertGreaterThan(notifications, 0, "clear() 没发通知")
        XCTAssertTrue(m.points.isEmpty)
    }

    /// `scaled(to:)` 整体替换 points，也要发通知。
    func testScaledPublishesObjectWillChange() {
        let m = RouteModel()
        m.add(GeoPoint(lat: 30.5, lon: 104.0))
        m.add(GeoPoint(lat: 30.6, lon: 104.1))

        var notifications = 0
        let token = m.objectWillChange.sink { _ in notifications += 1 }
        defer { token.cancel() }

        XCTAssertTrue(m.scaled(to: 3000))
        XCTAssertGreaterThan(notifications, 0, "scaled(to:) 没发通知")
    }
}