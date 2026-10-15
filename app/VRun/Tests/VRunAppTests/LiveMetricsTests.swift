import XCTest
import MapKit
import AppKit
@testable import VRunApp

// ══════════════════════════════════════════════════════════════════
//  LiveMetricsTests.swift —— 实时读数的**跨语言契约**与摄入逻辑
//
//  ★ 这组测试的存在理由（2026-10-15）
//
//      `vrun.session` 一直按 1 Hz 发 `tick` + `progress`，
//      但 Swift 侧压根没接 —— `SidecarEvent` 不解码那些字段，
//      `apply(_:)` 只认 `device` 事件。**两侧都不报错。**
//
//      也就是说「协议没对上」这件事可以潜伏很久，直到有人真去读实时数据。
//      ⇒ 光写 Swift 单测不够，必须有一份**Python 真的发过的**载荷摆在这，
//        让 Swift 去解码它。字段一改，两侧同时红。
//
//  ★ 为什么不把 golden 数值转抄进测试里（PreviewMathTests 那种写法）
//
//      转抄会引入一次**人工搬运**，而这次 bug 的本质就是搬运失败
//      （字段根本没人抄，因为压根没想到要抄）。
//      直接读 `tests/golden/session_events.json`，两边共用同一份真相。
// ══════════════════════════════════════════════════════════════════

final class LiveMetricsTests: XCTestCase {

    // MARK: - 基准

    private struct Golden: Decodable {
        struct Payload: Decodable {
            let t: Double
            let lat: Double
            let lon: Double
            let s: Double
            let v_target: Double
            let v_actual: Double
        }
        struct Progress: Decodable {
            let t: Double
            let lat: Double
            let lon: Double
            let s: Double
            let target_m: Double
            let v_actual: Double
            let v_target: Double
            let sigma_v: Double
            let sample_rate: Double
        }
        let tick: Payload
        let progress: Progress
    }

    /// 仓库根。`MainView.locateRepoRoot()` 靠 `scripts/resolve_python.sh` 上溯，
    /// 开发态与打包态都能用。
    private static var goldenURL: URL {
        MainView.locateRepoRoot().appendingPathComponent("tests/golden/session_events.json")
    }

    /// 从 golden 里取出某一段，**补上 `event` 键**后按 SidecarEvent 解码。
    ///
    /// golden 里存的是**载荷本体**（`session._emit` 收到的那个 dict），
    /// 而线上真正过 JSON 的是 `ipc.event()` 包过一层 `{"event": …}` 的帧。
    /// 这个差别本身就是当时能对不上的原因之一，所以在测试里显式补齐。
    private func decodeGoldenFrame(_ key: String) throws -> SidecarEvent {
        let raw = try JSONSerialization.jsonObject(
            with: Data(contentsOf: Self.goldenURL)) as! [String: Any]
        var frame = raw[key] as! [String: Any]
        frame["event"] = key
        return try JSONDecoder().decode(
            SidecarEvent.self, from: try JSONSerialization.data(withJSONObject: frame))
    }

    private func loadGolden() throws -> Golden {
        let url = Self.goldenURL
        try XCTSkipUnless(FileManager.default.fileExists(atPath: url.path),
                          "找不到基准 \(url.path)")
        return try JSONDecoder().decode(Golden.self, from: Data(contentsOf: url))
    }

    // MARK: - 跨语言契约：真解码 Python 发过的载荷

    /// ★ 这是本文件最重要的一条：直接解码**真实发出过**的 tick。
    func test能解码Python真实发出的tick() throws {
        let decoded = try decodeGoldenFrame("tick")

        XCTAssertEqual(decoded.event, "tick")
        XCTAssertNotNil(decoded.t)
        XCTAssertNotNil(decoded.s)
        XCTAssertNotNil(decoded.vTarget)
        XCTAssertNotNil(decoded.vActual)
        // tick **不带** target_m / sigma_v —— 这是刻意的，别把它当漏字段
        XCTAssertNil(decoded.targetM)
        XCTAssertNil(decoded.sigmaV)
        XCTAssertNotNil(decoded.location, "tick 应当能还原出坐标")
    }

    /// progress 实时载荷带全部 9 个字段。
    func test能解码Python真实发出的实时progress() throws {
        let decoded = try decodeGoldenFrame("progress")

        XCTAssertEqual(decoded.event, "progress")
        for name in ["t", "s", "targetM", "vTarget", "vActual", "sigmaV", "sampleRate"] {
            XCTAssertNotNil(decoded.value(forKey: name),
                            "实时 progress 缺字段 \(name) —— 协议与 Swift 不同步")
        }
        XCTAssertNotNil(decoded.location)
    }

    /// 数值必须与 Python 发的**一致**，不只是「非 nil」。
    /// 单位搞错（m vs km）是这条最容易犯的错。
    func test数值与Python逐字一致() throws {
        let g = try loadGolden()
        var m = LiveMetrics()
        let ev = try decodeGoldenFrame("progress")
        XCTAssertTrue(m.ingest(ev))

        XCTAssertEqual(m.elapsedS, g.progress.t, accuracy: 1e-9)
        XCTAssertEqual(m.walkedM, g.progress.s, accuracy: 1e-9)
        XCTAssertEqual(m.targetM, g.progress.target_m, accuracy: 1e-9)
        XCTAssertEqual(m.vActualKmh, g.progress.v_actual, accuracy: 1e-9)
        XCTAssertEqual(m.vTargetKmh, g.progress.v_target, accuracy: 1e-9)
        XCTAssertEqual(m.sigmaV, g.progress.sigma_v, accuracy: 1e-9)
        XCTAssertEqual(m.sampleRate, g.progress.sample_rate, accuracy: 1e-9)
    }

    // MARK: - 摄入逻辑

    /// ★ `progress` 事件名被重载：会话摘要**不能**被当成实时读数。
    ///
    /// 这是本次修复里最容易写错的一处 —— 写成 `case "progress":` 就完蛋，
    /// GUI 会显示一个不动的 0 km/h，比不显示更难查。
    func test会话摘要不会被当成实时读数() throws {
        // `run.start` 的真实应答形状：只有 phase，没有 sigma_v
        let json = """
        {"event": "progress", "phase": "running", "target_m": 431.8}
        """.data(using: .utf8)!
        let ev = try JSONDecoder().decode(SidecarEvent.self, from: json)

        var m = LiveMetrics()
        XCTAssertFalse(m.ingest(ev), "摘要事件被当成了实时读数")
        XCTAssertFalse(m.isLive)
    }

    func testdevice事件不会污染实时读数() throws {
        let json = """
        {"event": "device", "connected": true, "lat": 30.5, "lon": 103.9}
        """.data(using: .utf8)!
        let ev = try JSONDecoder().decode(SidecarEvent.self, from: json)

        var m = LiveMetrics()
        XCTAssertFalse(m.ingest(ev))
        XCTAssertFalse(m.isLive)
        XCTAssertNil(m.coordinate, "device 事件的坐标是设备位置，不是会话当前位置")
    }

    /// tick 不带 target_m / sigma_v —— **绝不能**用 0 覆盖上一条 progress 的值。
    /// 否则 HUD 上的「路线全长」会在两个 tick 之间闪回 0。
    func test_tick不会把全长和抖动清零() throws {
        let prog = """
        {"event":"progress","t":10,"lat":30.5,"lon":103.9,"s":30,
         "target_m":431.8,"v_actual":10.1,"v_target":10.0,"sigma_v":1.96,"sample_rate":1.0}
        """.data(using: .utf8)!
        let tick = """
        {"event":"tick","t":11,"lat":30.5,"lon":103.9,"s":33,"v_target":10.0,"v_actual":10.2}
        """.data(using: .utf8)!

        var m = LiveMetrics()
        XCTAssertTrue(m.ingest(try JSONDecoder().decode(SidecarEvent.self, from: prog)))
        XCTAssertTrue(m.ingest(try JSONDecoder().decode(SidecarEvent.self, from: tick)))

        XCTAssertEqual(m.targetM, 431.8, "tick 把路线全长清掉了")
        XCTAssertEqual(m.sigmaV, 1.96, "tick 把表观抖动清掉了")
        XCTAssertEqual(m.walkedM, 33)
        XCTAssertEqual(m.elapsedS, 11)
    }

    // MARK: - 派生量与文案

    func test剩余距离与完成度() {
        var m = LiveMetrics()
        m.targetM = 3000
        m.walkedM = 750
        XCTAssertEqual(m.remainingM, 2250, accuracy: 1e-9)
        XCTAssertEqual(m.fraction, 0.25, accuracy: 1e-9)
        XCTAssertEqual(m.progressText, "750 m / 3.00 km")
    }

    /// 走过量不能超过全长（浮点与侧车取整都可能给出一点点超出）。
    func test走过头时钳住不越界() {
        var m = LiveMetrics()
        m.targetM = 1000
        m.walkedM = 1000.4
        XCTAssertEqual(m.remainingM, 0)
        XCTAssertEqual(m.fraction, 1.0)
    }

    /// 路线未知时不能显示成「已完成 0%」或除以零。
    func test路线未知时派生量退化为0() {
        let m = LiveMetrics()
        XCTAssertEqual(m.remainingM, 0)
        XCTAssertEqual(m.fraction, 0)
        XCTAssertEqual(m.naiveRemainingS, 0)
        XCTAssertEqual(m.paceMinPerKm, 0)
    }

    /// 停住时不能除以零 —— 会得到 inf 或 NaN 显示在界面上。
    func test速度为0时配速与剩余时间不会炸() {
        var m = LiveMetrics()
        m.targetM = 3000
        m.vActualKmh = 0
        XCTAssertEqual(m.paceMinPerKm, 0)
        XCTAssertEqual(m.naiveRemainingS, 0)
        XCTAssertTrue(m.paceText.hasSuffix("—"))
    }

    func test配速换算() {
        var m = LiveMetrics()
        m.vActualKmh = 12.0
        XCTAssertEqual(m.paceMinPerKm, 5.0, accuracy: 1e-9)   // 60/12
    }

    func test未收到实时数据时显示破折号而不是0() {
        let m = LiveMetrics()
        XCTAssertEqual(m.speedText, "—")
        XCTAssertEqual(m.sigmaText, "—")
        XCTAssertEqual(m.targetSpeedText, "—")
    }

    func test冻结保留最后一帧() {
        var m = LiveMetrics()
        m.walkedM = 1234
        m.isLive = true
        m.freeze()
        XCTAssertEqual(m.walkedM, 1234, "冻结不该抹掉读数 —— 用户要靠它核对停在哪儿")
        XCTAssertFalse(m.isLive)
    }

    func test重置清空一切() {
        var m = LiveMetrics()
        m.walkedM = 1234
        m.targetM = 3000
        m.coordinate = GeoPoint(lat: 30.5, lon: 103.9)
        m.reset()
        XCTAssertEqual(m.walkedM, 0)
        XCTAssertEqual(m.targetM, 0)
        XCTAssertNil(m.coordinate)
        XCTAssertFalse(m.isLive)
    }

    // MARK: - 地图上的当前位置

    /// 挪动阈值：1.5 m。停了就不该反复打扰 MapKit。
    func test位置变化阈值() {
        let a = GeoPoint(lat: 30.5535, lon: 103.9910)
        // 成都一带经度方向要乘 cos(lat)，1e-5 度经度 ≈ 0.94 m
        let b = GeoPoint(lat: 30.5535, lon: 103.9910 + 0.00002)  // ≈ 1.9 m
        XCTAssertTrue(RouteCanvas.positionNeedsUpdate(from: a, to: b))
        XCTAssertFalse(RouteCanvas.positionNeedsUpdate(from: a, to: a))
        let tiny = GeoPoint(lat: 30.5535, lon: 103.9910 + 0.000001)  // ≈ 0.09 m
        XCTAssertFalse(RouteCanvas.positionNeedsUpdate(from: a, to: tiny))
    }

    /// ⚠️ 当前位置的 annotation 必须 KVO-compliant，否则图上的点**永远不动**
    /// —— 而它不报错，只是静静地不动。
    func test当前位置annotation的坐标变化能被KVO观察到() {
        let ann = RouteCanvas.CurrentPositionAnnotation(point: GeoPoint(lat: 30.0, lon: 103.0))
        var observed = false
        let token = ann.observe(\.coordinate, options: [.new]) { _, _ in observed = true }
        ann.coordinate = CLLocationCoordinate2D(latitude: 30.1, longitude: 103.1)
        token.invalidate()
        XCTAssertTrue(observed, "coordinate 不是 KVO-compliant —— MapKit 不会跟着动")
    }
}

// MARK: - 反射小助手

private extension SidecarEvent {
    /// 按 Swift 属性名取可选项，用于「9 个字段都在不在」那种批量断言。
    ///
    /// 用 Mirror 而不是逐个 `decoded.t` 写死：字段一多，逐个写会漏，
    /// 而漏掉的那个恰恰是协议漂移最先暴露的地方。
    /// （已验证：Mirror 取到的 `Optional.none` 再 `as? Double` 确实得到 nil，
    ///   不会因为 Any 装 Optional 而假阳性。）
    func value(forKey key: String) -> Double? {
        Mirror(reflecting: self).children.first { $0.label == key }?.value as? Double
    }
}

// ══════════════════════════════════════════════════════════════════
//  SessionController 层的集成：JSON 行 → SidecarEvent → LiveMetrics
//
//  ★ 为什么已有 decode 测试还要这一层
//
//      前面的测试验的是「每一步都对」。
//      这一组验的是「**这些步骤连起来**还对」——
//      也就是当年把数据线掐死的那一层：SessionController.apply
//      压根没调 ingest，两步各自都对，中间那步断了。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class LiveMetricsWiringTests: XCTestCase {

    private func makeController() -> SessionController {
        SessionController(sidecar: SidecarClient(repoRoot: URL(fileURLWithPath: ".")),
                          route: RouteModel())
    }

    func test一串tick与progress能把实时读数喂进去() throws {
        let c = makeController()
        XCTAssertFalse(c.live.isLive, "还没收到任何事件时不该显示实时读数")

        func apply(_ json: String) throws {
            c.apply(try JSONDecoder().decode(
                SidecarEvent.self, from: json.data(using: .utf8)!))
        }

        try apply("""
        {"event":"progress","t":0,"lat":30.5535,"lon":103.9910,"s":0,
         "target_m":3000,"v_actual":0,"v_target":0,"sigma_v":1.96,"sample_rate":1.0}
        """)
        try apply("""
        {"event":"tick","t":1,"lat":30.5535,"lon":103.9910,"s":2.8,
         "v_target":10.0,"v_actual":10.1}
        """)
        try apply("""
        {"event":"tick","t":2,"lat":30.5536,"lon":103.9911,"s":5.6,
         "v_target":10.0,"v_actual":10.2}
        """)

        XCTAssertTrue(c.live.isLive)
        XCTAssertEqual(c.live.elapsedS, 2, accuracy: 1e-9)
        XCTAssertEqual(c.live.walkedM, 5.6, accuracy: 1e-9)
        XCTAssertEqual(c.live.targetM, 3000, accuracy: 1e-9)
        XCTAssertEqual(c.live.vActualKmh, 10.2, accuracy: 1e-9)
        XCTAssertEqual(c.live.sigmaV, 1.96, accuracy: 1e-9)
        XCTAssertNotNil(c.live.coordinate)
    }

    /// device 事件与实时读数**互不干扰** —— 这是两件事，
    /// 当年把它们混在一个 apply 里，正好是一个漏一个的根源。
    func testdevice事件不影响实时读数() throws {
        let c = makeController()
        c.apply(try JSONDecoder().decode(SidecarEvent.self, from: """
        {"event":"device","connected":true,"device_name":"iPhone","lat":30.5,"lon":103.9}
        """.data(using: .utf8)!))
        XCTAssertTrue(c.deviceConnected, "device 事件本身必须照常生效")
        XCTAssertFalse(c.live.isLive, "device 事件不该点亮实时读数")
        XCTAssertNil(c.live.coordinate)
    }

    /// 开始新会话前必须清干净 —— 否则上一次的距离会一直挂在 HUD 上。
    func testprepare会清掉上一次的读数() {
        let c = makeController()
        c.apply(try! JSONDecoder().decode(SidecarEvent.self, from: """
        {"event":"tick","t":600,"lat":30.5,"lon":103.9,"s":1800,
         "v_target":10.0,"v_actual":10.0}
        """.data(using: .utf8)!))
        XCTAssertEqual(c.live.walkedM, 1800)

        c.prepare()
        XCTAssertEqual(c.live.walkedM, 0)
        XCTAssertFalse(c.live.isLive)
        XCTAssertNil(c.live.coordinate)
    }

    /// stop 之后**冻结**而不是清零 —— 用户要靠它核对「停在哪儿、走了多远」。
    func teststop冻结读数而不是清零() {
        let c = makeController()
        c.apply(try! JSONDecoder().decode(SidecarEvent.self, from: """
        {"event":"tick","t":600,"lat":30.5,"lon":103.9,"s":1800,
         "v_target":10.0,"v_actual":10.0}
        """.data(using: .utf8)!))
        c.stop()
        XCTAssertEqual(c.live.walkedM, 1800, "stop 把读数抹掉了，用户就没法核对终点")
        XCTAssertFalse(c.live.isLive, "冻结后 HUD 的运行中区块应收起")
    }
}

// ══════════════════════════════════════════════════════════════════
//  MapKit 真身测试 —— 当前位置 annotation 会进 MapKit 的装配路径
//
//  ★ 为什么这条不能省（F13 的教训）
//
//      上一轮的闪退就发生在「把对象交给 MapKit」的那一步，
//      而当时的 26 条纯逻辑测试全绿。
//      当前位置 annotation 走的是**同一条**路径（addAnnotation → updateVisibleAnnotations），
//      所以它必须有一条真喂 MapKit 的测试。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class CurrentPositionMapKitTests: XCTestCase {

    func test加当前位置并移动视野不会崩() {
        let map = PenMapView(frame: NSRect(x: 0, y: 0, width: 480, height: 480))
        let win = NSWindow(contentRect: map.frame, styleMask: [.titled, .closable],
                           backing: .buffered, defer: false)
        win.contentView = map
        win.orderFront(nil)
        defer {
            map.delegate = nil
            win.orderOut(nil)
            win.contentView = nil
        }

        let canvas = RouteCanvas(isPenActive: false, route: RouteModel(),
                                 currentPosition: nil, onPick: { _ in })
        let coord = canvas.makeCoordinator()
        withExtendedLifetime(coord) {
            map.delegate = coord
            map.setRegion(MKCoordinateRegion(
                center: CLLocationCoordinate2D(latitude: 30.5554, longitude: 103.9907),
                span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)),
                animated: false)

            // 与 updateNSView 里做的事一致
            let ann = RouteCanvas.CurrentPositionAnnotation(
                point: GeoPoint(lat: 30.5554, lon: 103.9907))
            map.addAnnotation(ann)
            ann.coordinate = CLLocationCoordinate2D(latitude: 30.5560, longitude: 103.9910)
            map.setCenter(CLLocationCoordinate2D(latitude: 30.556, longitude: 103.991),
                          animated: false)

            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            map.layoutSubtreeIfNeeded()
        }
    }

    /// 当前位置与绿圈、路线点**坐标重合**时的形态
    /// （刚 `prepare` 完还没推进，两者在同一个点）。
    func test三个annotation坐标重合也不会崩() {
        let map = PenMapView(frame: NSRect(x: 0, y: 0, width: 480, height: 480))
        let win = NSWindow(contentRect: map.frame, styleMask: [.titled, .closable],
                           backing: .buffered, defer: false)
        win.contentView = map
        win.orderFront(nil)
        defer {
            map.delegate = nil
            win.orderOut(nil)
            win.contentView = nil
        }

        let canvas = RouteCanvas(isPenActive: false, route: RouteModel(),
                                 currentPosition: nil, onPick: { _ in })
        let coord = canvas.makeCoordinator()
        withExtendedLifetime(coord) {
            map.delegate = coord
            map.setRegion(MKCoordinateRegion(
                center: CLLocationCoordinate2D(latitude: 30.5554, longitude: 103.9907),
                span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01)),
                animated: false)

            let p = GeoPoint(lat: 30.5554, lon: 103.9907)
            map.addAnnotation(RouteCanvas.PointAnnotation(point: p, index: 0))
            map.addAnnotation(RouteCanvas.DeviceAnnotation(point: p))
            map.addAnnotation(RouteCanvas.CurrentPositionAnnotation(point: p))
            map.setCenter(CLLocationCoordinate2D(latitude: 30.556, longitude: 103.991),
                          animated: false)

            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            map.layoutSubtreeIfNeeded()
        }
    }
}
