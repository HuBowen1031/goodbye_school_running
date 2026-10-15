import XCTest
import MapKit
@testable import VRunApp

// ══════════════════════════════════════════════════════════════════
//  PenClickTests —— 钢笔「点一下加一个点」的判定逻辑
//
//  ★ 这组测试存在的理由（2026-10-14 用户实测报障）
//
//     用户点地图没反应。查下来根因是：
//     原实现把点击挂在 `MKMapViewDelegate` 的
//         func mapView(_:mouseDown:) -> Bool
//     上，而**这个方法不存在于协议里** —— 全 MapKit 头文件搜
//     `mouseDown` / `mouseUp` / `handle:` 零命中。
//     它只是普通 NSObject 方法，MapKit 永远不会调用。
//     而它**能编译通过**，于是「钢笔亮着、提示写着、就是不响应」。
//
//     教训：**编译成功不能证明某个方法是框架回调**。
//     凡是「接第三方框架回调」的代码，都要有验证它被调用过的手段。
//
//  ★ 这里把判定逻辑抽成 `feedClickState(_:at:)` 就是为了可测：
//     否则只能靠真实鼠标事件验证，CI 上跑不了，
//     而这类「不报错、只是没反应」的 bug 恰恰最需要测试。
// ══════════════════════════════════════════════════════════════════

final class PenClickTests: XCTestCase {

    private func makeMap() -> PenMapView {
        // MKMapView 的实例化不需要 RunLoop 或主线程之外的东西，
        // 这里只测纯状态机，不碰地图渲染。
        PenMapView(frame: CGRect(x: 0, y: 0, width: 400, height: 400))
    }

    // MARK: - 正常点击：应当落一个点

    func testCleanClickCommits() {
        let m = makeMap()
        let p = CGPoint(x: 100, y: 100)
        XCTAssertFalse(m.feedClickState(.began, at: p), "began 不该提交")
        XCTAssertTrue(m.feedClickState(.ended, at: p), "干净的一次点击必须落点")
    }

    func testTinyJitterStillCountsAsClick() {
        // 手抖 2 px（< dragSlop 4 pt）不该被当成拖地图。
        // 太敏感的话钢笔会「时灵时不灵」，比完全不响应更难排查。
        let m = makeMap()
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))
        m.feedClickState(.moved, at: CGPoint(x: 102, y: 101))
        XCTAssertTrue(m.feedClickState(.ended, at: CGPoint(x: 101, y: 100)))
    }

    // MARK: - 拖动：不得落点

    func testDragBeyondSlopDoesNotCommit() {
        // 钢笔语义是「点一下加一个点」。按住拖动是在平移地图，
        // 顺手加一个点会让用户莫名其妙多出一段路线。
        let m = makeMap()
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))
        m.feedClickState(.moved, at: CGPoint(x: 180, y: 100))
        XCTAssertFalse(m.feedClickState(.ended, at: CGPoint(x: 180, y: 100)),
                       "拖动后松手不得落点")
    }

    func testDragJustUnderSlopStillCommits() {
        // 边界：正好等于阈值仍算点击。
        let m = makeMap()
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))
        m.feedClickState(.moved, at: CGPoint(x: 104, y: 100))
        XCTAssertTrue(m.feedClickState(.ended, at: CGPoint(x: 104, y: 100)))
    }

    // MARK: - 状态机的健壮性

    func testEndedWithoutBeganDoesNotCommit() {
        // 鼠标从别处按下、在地图上松开（少见但可能）。
        // 绝不能凭空落一个点。
        let m = makeMap()
        XCTAssertFalse(m.feedClickState(.ended, at: CGPoint(x: 50, y: 50)))
    }

    func testDoubleClickProducesTwoPoints() {
        // 连续点两下 = 两个点。钢笔就是靠这个加点的。
        let m = makeMap()
        var committed = 0
        for x in [100.0, 160.0] {
            m.feedClickState(.began, at: CGPoint(x: x, y: 100))
            if m.feedClickState(.ended, at: CGPoint(x: x, y: 100)) { committed += 1 }
        }
        XCTAssertEqual(committed, 2)
    }

    func testCancelledResetsState() {
        let m = makeMap()
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))
        m.feedClickState(.cancelled, at: CGPoint(x: 100, y: 100))
        // 取消后紧接着松手 —— 不该补一个点出来
        XCTAssertFalse(m.feedClickState(.ended, at: CGPoint(x: 100, y: 100)))
    }

    func testTurningPenOffMidPressDropsTheClick() {
        // ⚠️ 状态切换必须清掉半途按下的残留。
        //   不清的话「钢笔开着按下 → 用户点掉钢笔 → 松手」会漏出一个点，
        //   而钢笔已经关了 —— 用户会看到一个无法解释的点冒出来。
        let m = makeMap()
        m.penHandler = { _ in }
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))
        m.penHandler = nil                       // 关掉钢笔（nil ← 非 nil，是切换）
        XCTAssertFalse(m.feedClickState(.ended, at: CGPoint(x: 100, y: 100)),
                       "关掉钢笔后残留的按下不得补出点")
    }

    // ★★ 2026-10-14 实测的间歇性丢点击，回归护栏
    func testRebuildingHandlerMidPressMustNotDropTheClick() {
        // SwiftUI 会在**任意两次点击之间**重绘（estimate 的 250 ms 去抖就会触发），
        // `updateNSView` 每次都重建 penHandler 闭包。
        // 若 didSet 无条件清理 pressOrigin，这次按压就被静默丢弃 ——
        // 用户体验是「有时候点得中，有时候点不动」，而且**没有任何报错**。
        let m = makeMap()
        m.penHandler = { _ in }
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))

        // 模拟一次 SwiftUI 重绘带来的闭包重建（两边都非 nil ⇒ 不是开关切换）
        m.penHandler = { _ in }

        XCTAssertTrue(m.feedClickState(.ended, at: CGPoint(x: 100, y: 100)),
                      "闭包重建把这次点击吞了 —— 典型的「间歇性点不动」")
    }

    func testManyPressesWithHandlerRebuildBetweenThemAllWork() {
        // 连续点击，每次之间都重建一次 handler（最贴近真实的点击节奏）。
        let m = makeMap()
        var committed = 0
        for x in stride(from: 100.0, through: 400.0, by: 100.0) {
            m.penHandler = { _ in }
            m.feedClickState(.began, at: CGPoint(x: x, y: 100))
            m.penHandler = { _ in }               // 点击过程中又重建
            if m.feedClickState(.ended, at: CGPoint(x: x, y: 100)) { committed += 1 }
        }
        XCTAssertEqual(committed, 4, "4 次点击必须全部落点")
    }

    func testNewPressAfterDroppedOneStillWorks() {
        // 上一条清状态的副作用不能把状态机卡死。
        let m = makeMap()
        m.feedClickState(.began, at: CGPoint(x: 100, y: 100))
        m.penHandler = nil
        _ = m.feedClickState(.ended, at: CGPoint(x: 100, y: 100))

        m.penHandler = { _ in }
        m.feedClickState(.began, at: CGPoint(x: 200, y: 200))
        XCTAssertTrue(m.feedClickState(.ended, at: CGPoint(x: 200, y: 200)),
                      "状态机被卡死了 —— 之后再点就没反应")
    }
}

// ══════════════════════════════════════════════════════════════════
//  GreenDotAnnotationView —— 崩溃回归护栏
//
//  ★ 用户实测崩溃报告（2026-10-14 22:31）
//        abort()
//        MapKit  -[MKAnnotationView clusteringIdentifier]
//        MapKit  -[MKMapView addAnnotationRepresentation:allowAnimation:]
//        MapKit  -[MKAnnotationContainerView addAnnotationView:allowAnimation:]
//
//     根因：直接用抽象基类 `MKAnnotationView`。
//     MapKit 在 `addAnnotationView:` 内部会查 `clusteringIdentifier`，
//     而基类把它实现成了「抛异常」（它是留给子类覆盖的属性）。
//
//     ⚠️ 这类 bug 单靠肉眼 review 极难发现 —— 类型完全合法、
//        编译零警告、只有真跑起来点一下才会炸。
//        所以这里钉一条结构性断言：绿圈**必须**是具体子类。
// ══════════════════════════════════════════════════════════════════

final class GreenDotAnnotationViewTests: XCTestCase {

    func testGreenDotIsAConcreteSubclassNotTheAbstractBase() {
        let ann = RouteCanvas.DeviceAnnotation(point: GeoPoint(lat: 30.5, lon: 104.0))
        let v = GreenDotAnnotationView(annotation: ann, reuseIdentifier: "device")

        XCTAssertFalse(type(of: v) == MKAnnotationView.self,
                       "用了抽象基类 —— 点地图时会 abort 崩溃")

        // 基类的抽象属性在子类里必须有确定值（nil = 不参与聚类）
        XCTAssertNil(v.clusteringIdentifier)
    }

    func testGreenDotViewIsReusableThroughDequeue() {
        // viewFor 里按 GreenDotAnnotationView 做向下转型 ——
        // 类型对不上就会每次都新建，白白丢掉复用。
        let ann = RouteCanvas.DeviceAnnotation(point: GeoPoint(lat: 30.5, lon: 104.0))
        let v = GreenDotAnnotationView(annotation: ann, reuseIdentifier: "device")
        let asBase: MKAnnotationView = v
        XCTAssertTrue(asBase is GreenDotAnnotationView,
                      "dequeue 的向下转型必须成立")
    }
}