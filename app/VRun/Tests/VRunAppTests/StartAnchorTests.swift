import XCTest
@testable import VRunApp

// ══════════════════════════════════════════════════════════════════
//  StartAnchorTests —— 绿圈（路线起点）的取值规则
//
//  ★ 为什么这些测试必须存在（`user_found_problem.md` 第 1 条）
//     用户原话：「绿点显然不会出现（这里要测试下绿点能不能正常显示，
//     我记得这个测试是没有被做过的。）」
//
//     事实核对结果比「没测」更糟：`RouteModel.deviceLocation`
//     **全项目没有任何一处赋值**，而绿圈只读它 ——
//     也就是说绿圈**从来就不可能出现**，绿点测试从来没被做过只是表象。
//
//  ★ 这组测试守的是一条**纯规则**，不碰任何 UI：
//     绿圈要么落在设备位置，要么落在用户点的第一个点，
//     永远不允许「两者都没有却还画一个绿圈」。
//     这正是最初坏掉的那一环（未连接 → 无锚点 → 用户无法起笔）。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class StartAnchorTests: XCTestCase {

    private let device = GeoPoint(lat: 30.5535, lon: 103.9910)
    private let firstClick = GeoPoint(lat: 30.6000, lon: 104.1000)

    private func makeRoute(points: [GeoPoint] = [], deviceLocation: GeoPoint? = nil) -> RouteModel {
        let m = RouteModel()
        for p in points { m.add(p) }
        m.deviceLocation = deviceLocation
        return m
    }

    // MARK: - 没连手机：第一个点就是起点 ★ 用户要的那条

    func testFirstClickBecomesAnchorWhenDeviceMissing() {
        let m = makeRoute(points: [firstClick])
        let a = m.startAnchor(deviceConnected: false)
        XCTAssertNotNil(a, "没连手机时必须有锚点，否则用户无法起笔")
        XCTAssertEqual(a?.lat, firstClick.lat)
        XCTAssertEqual(a?.lon, firstClick.lon)
    }

    func testUndoingTheFirstPointMovesAnchorAway() {
        let m = makeRoute(points: [firstClick, GeoPoint(lat: 30.61, lon: 104.11)])
        XCTAssertEqual(m.startAnchor(deviceConnected: false)?.lat, firstClick.lat)

        // 起点被撤销后，绿圈必须跟着退到新的第一个点，
        // 而不是继续停在一个已经不存在的点上。
        m.undo(); m.undo()
        XCTAssertNil(m.startAnchor(deviceConnected: false),
                     "点被全部撤销后不该再有锚点")
    }

    func testClearingRouteRemovesAnchor() {
        let m = makeRoute(points: [firstClick])
        XCTAssertNotNil(m.startAnchor(deviceConnected: false))
        m.clear()
        XCTAssertNil(m.startAnchor(deviceConnected: false),
                     "清空路线后绿圈必须撤掉，否则它会冒充起点")
    }

    func testEmptyRouteWithoutDeviceHasNoAnchor() {
        // 这是「还没画」的状态 —— 绿圈不该凭空出现，
        // 此时界面该提示用户点一下地图。
        let m = makeRoute()
        XCTAssertNil(m.startAnchor(deviceConnected: false))
    }

    // MARK: - 连上手机：设备位置优先

    func testDeviceLocationWinsWhenConnected() {
        let m = makeRoute(points: [firstClick], deviceLocation: device)
        let a = m.startAnchor(deviceConnected: true)
        XCTAssertEqual(a?.lat, device.lat, "已连接时应以设备位置为起点")
        XCTAssertEqual(a?.lon, device.lon)
    }

    func testDeviceLocationUsedEvenBeforeAnyPointIsDrawn() {
        // 连上了、也报过位置、但用户还没画 —— 绿圈应直接落在设备位置，
        // 这样「从 iPhone 当前位置起笔」这句话才是真的。
        let m = makeRoute(deviceLocation: device)
        XCTAssertEqual(m.startAnchor(deviceConnected: true)?.lat, device.lat)
    }

    func testConnectedButNoLocationFallsBackToFirstPoint() {
        // 连上了但从没注入过 / 刚 clear 过 —— 位置未知。
        // 此时绿圈退回用户点，但不能凭空编一个位置。
        let m = makeRoute(points: [firstClick], deviceLocation: nil)
        let a = m.startAnchor(deviceConnected: true)
        XCTAssertEqual(a?.lat, firstClick.lat, "位置未知时应退回第一个点")
    }

    func testStaleDeviceLocationIsIgnoredAfterDisconnect() {
        // ★ 设备被拔掉后，之前缓存的 deviceLocation **必须**失效。
        //   不失效的话绿圈会停在一个已经管不到的位置上，
        //   用户会以为「手机还在那儿」，照着画出一条错的起点。
        let m = makeRoute(points: [firstClick], deviceLocation: device)
        XCTAssertEqual(m.startAnchor(deviceConnected: true)?.lat, device.lat)
        // 拔线 —— deviceLocation 由 SessionController 清成 nil
        m.deviceLocation = nil
        XCTAssertEqual(m.startAnchor(deviceConnected: false)?.lat, firstClick.lat,
                       "断连后不得继续用缓存的设备位置")
    }

    // MARK: - 缩放 / 编辑不应破坏锚点语义

    func testAnchorSurvivesScaling() {
        let m = makeRoute(points: [firstClick, GeoPoint(lat: 30.62, lon: 104.12)])
        XCTAssertTrue(m.scaled(to: 1000))
        let a = m.startAnchor(deviceConnected: false)
        XCTAssertNotNil(a, "缩放后锚点仍应是路线的第一个点")
        XCTAssertEqual(a?.lat, m.points.first?.lat)
    }

    // MARK: - 缩放必须**固定起点** ★ 跨语言契约

    /// ★ 回归：``scaled(to:)`` 必须把**起点钉在原地**。
    ///
    /// 原因是一条链：
    ///   绿圈 = 路线起点（``startAnchor``）→ 侧车注入 ``route.point_at(0)``。
    ///   若缩放绕**质心**进行，起点会被推走 ——
    ///   画一个 30 m 直径的圈再缩放到 3000 m，起点漂 **451 m**（实测），
    ///   而界面**不报任何错**。
    ///
    /// Python 侧 ``Route.scaled_to`` 同样改了，两边必须一致。
    func testScalingKeepsStartPointFixed() {
        let second = GeoPoint(lat: 30.6005, lon: 104.1005)
        let m = makeRoute(points: [firstClick, second])
        let before = m.points[0]
        XCTAssertTrue(m.scaled(to: 3000))
        XCTAssertEqual(m.points[0].lat, before.lat, accuracy: 1e-12,
                       "缩放后起点漂了 —— 注入的将是另一个地方")
        XCTAssertEqual(m.points[0].lon, before.lon, accuracy: 1e-12)
    }

    /// 反复缩放也不该让起点漂 —— 迭代修正里最容易丢的就是锚点。
    func testRepeatedScalingKeepsStartPointFixed() {
        let second = GeoPoint(lat: 30.6005, lon: 104.1005)
        let m = makeRoute(points: [firstClick, second])
        let before = m.points[0]
        for target in [1000.0, 5000.0, 300.0, 12000.0] {
            XCTAssertTrue(m.scaled(to: target))
            XCTAssertEqual(m.points[0].lat, before.lat, accuracy: 1e-9)
        }
    }
}

// ══════════════════════════════════════════════════════════════════
//  SidecarEvent 的位置解析
//
//  ★ 另一条会静默失效的链路
//     绿点「不出现」有两条独立原因，两条都在这里：
//       ① Python 没把 lat/lon 放进事件载荷（已在 test_device.py 覆盖）
//       ② Swift 没解出这两个字段（这里覆盖）
//     任何一条断了，症状**完全一样**：绿圈不见了，日志一切正常。
// ══════════════════════════════════════════════════════════════════

final class SidecarEventLocationTests: XCTestCase {

    private func decode(_ json: String) throws -> SidecarEvent {
        try JSONDecoder().decode(SidecarEvent.self, from: Data(json.utf8))
    }

    func testDecodesLastInjectedPosition() throws {
        let ev = try decode(#"{"event":"device","connected":true,"lat":30.5535,"lon":103.991}"#)
        XCTAssertEqual(ev.lat, 30.5535)
        XCTAssertEqual(ev.location?.lon, 103.991)
    }

    func testMissingCoordinatesDecodeToNil() throws {
        // 清理之后 Python 会发 null —— 必须解成 nil 而不是 0
        let ev = try decode(#"{"event":"device","connected":true,"lat":null,"lon":null}"#)
        XCTAssertNil(ev.location)
    }

    func testHalfPairIsRejected() throws {
        // ⚠️ 只给纬度、漏了经度是最危险的形态：
        //    经度缺省 0 会把绿圈放到几内亚湾外海，比「没有绿圈」更糟。
        //    走真实解码路径 —— 不要另写一份副本逻辑来"测"它，
        //    那测的是副本，真实实现坏了也照样绿。
        let ev = try decode(#"{"event":"device","connected":true,"lat":30.5535}"#)
        XCTAssertNil(ev.lon)
        XCTAssertNil(ev.location, "只有一个坐标时不得产出位置")

        let ev2 = try decode(#"{"event":"device","connected":true,"lon":103.991}"#)
        XCTAssertNil(ev2.lat)
        XCTAssertNil(ev2.location)
    }

    func testDecodesDeviceMetadata() throws {
        let ev = try decode("""
        {"event":"device","connected":true,"device_name":"iPhone 18,1",
         "ios_version":"27.0.1","tunnel_up":true,"phase":"ready",
         "injected":120,"dropped":2,"error":null}
        """)
        XCTAssertEqual(ev.connected, true)
        XCTAssertEqual(ev.deviceName, "iPhone 18,1")
        XCTAssertEqual(ev.iosVersion, "27.0.1")
        XCTAssertEqual(ev.tunnelUp, true)
        XCTAssertEqual(ev.injected, 120)
        XCTAssertEqual(ev.dropped, 2)
    }

    func testDisconnectedEventCarriesReason() throws {
        // 没连上时 Python 会带 error 文本 —— GUI 要把它显示出来，
        // 否则用户只知道「没连」，不知道是没插还是没配对。
        let ev = try decode(#"{"event":"device","connected":false,"error":"未配对"}"#)
        XCTAssertEqual(ev.connected, false)
        XCTAssertEqual(ev.error, "未配对")
    }
}