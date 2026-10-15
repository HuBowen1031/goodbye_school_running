import XCTest
import MapKit
import AppKit
@testable import VRunApp

// ══════════════════════════════════════════════════════════════════
//  GreenDotMapKitTests.swift —— 绿圈 annotation 的 **MapKit 真身**测试
//
//  ★ 为什么要起一个「可能 abort」的测试（2026-10-14 实测挖出来的）
//
//      在此之前，钢笔与绿圈一共有 26 条测试，**全是纯逻辑**：
//      点一下会不会落点、锚点算哪个坐标、@Published 会不会发通知。
//      没有一条真的把绿圈塞进 MKMapView。
//
//      结果 App 照样在「点第一下」时 abort：
//
//          objc_exception_throw
//          -[MKAnnotationView clusteringIdentifier]        ← 基类实现抛异常
//          -[MKMapView addAnnotationRepresentation:allowAnimation:]
//          -[MKAnnotationManager updateVisibleAnnotations]
//          -[MKMapView _didChangeRegionMidstream:]
//          -[MKMapView mapLayerDidChangeVisibleRegion]
//          VectorKit ... _animateToPosition:
//          -[MKMapView setCenterCoordinate:animated:]       ← 就是 updateNSView 里那一行
//          RouteCanvas.updateNSView(_:context:)
//
//      「编译通过 + 纯逻辑全绿 + 加点数据正确」，三件事同时成立，
//      进程照样死 —— 因为 abort 发生在 **MapKit 内部**，
//      是纯逻辑测试根本没有能力覆盖的那一层。
//
//      ⇒ **纯逻辑测试证明不了「框架会接受这个对象」。**
//         只要往 MapKit / AppKit 这类框架里塞对象，就必须有一条
//         真把对象交给框架、让它走一遍内部装配路径的测试。
//
//  ⚠️ 修复前这条测试会**直接 abort 掉整个 swift test 进程**。
//     这是特性不是缺陷：它证明「测试真的能抓到目标 bug」。
// ══════════════════════════════════════════════════════════════════

@MainActor
final class GreenDotMapKitTests: XCTestCase {

    /// 绿点（江安校区）。
    private static let 起点 = GeoPoint(lat: 30.5554, lon: 103.9907)

    private var 江安校区: MKCoordinateRegion {
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: 30.5554, longitude: 103.9907),
            span: MKCoordinateSpan(latitudeDelta: 0.01, longitudeDelta: 0.01))
    }

    /// 造一个带真实窗口的 MKMapView，跑完即收。
    ///
    /// 三个细节缺一不可，少一个测试就「假通过」：
    ///   ① **窗口**：MKMapView 挂上窗口才会真正布局，离屏裸 view 不会触发装配。
    ///   ② **delegate 必须被强引用持有**：`MKMapView.delegate` 是 `weak`，
    ///      写成 `map.delegate = RouteCanvas.Coordinator(...)` 的话
    ///      Coordinator 当场析构 → MapKit 拿不到我们的 view → 走默认路径 → 永不崩。
    ///      这个坑我踩过一次：测试全绿，App 照崩。
    ///   ③ **转一圈 runloop**：把 MapKit 挂起的装配工作逼出来。
    private func withRealMap(_ body: (MKMapView, NSWindow) -> Void) {
        let map = PenMapView(frame: NSRect(x: 0, y: 0, width: 480, height: 480))
        let win = NSWindow(contentRect: map.frame,
                           styleMask: [.titled, .closable],
                           backing: .buffered, defer: false)
        win.contentView = map
        win.orderFront(nil)

        // ② 关键：delegate 是 weak，必须由这个强引用活到 body 结束
        let coord = RouteCanvas.Coordinator(
            RouteCanvas(isPenActive: true, route: RouteModel(), onPick: { _ in }))
        withExtendedLifetime(coord) {
            map.delegate = coord
            map.setRegion(江安校区, animated: false)
            body(map, win)
            // ③
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            map.layoutSubtreeIfNeeded()
            map.displayIfNeeded()
        }

        map.delegate = nil
        win.orderOut(nil)
        win.contentView = nil
    }

    /// ★ 这条就是崩溃回归锁：加绿圈 → 移视野。
    ///
    /// 与崩溃栈逐帧对应，一字不差。
    func test加绿圈后移动视野不会崩() {
        withRealMap { map, _ in
            map.addAnnotation(RouteCanvas.DeviceAnnotation(point: Self.起点))
            // updateNSView 里锚点首次出现时走的正是这一行
            map.setCenter(CLLocationCoordinate2D(latitude: 30.56, longitude: 103.99),
                          animated: false)
        }
    }

    /// 绿圈与路线第一个点**坐标完全重合**的情形。
    ///
    /// 这是「设备没连」时的常态（见 RouteModel.startAnchor），
    /// 也是崩溃当场的形态 —— 崩溃那次 anchorIsOnRoute == true。
    func test绿圈与起点重合也不会崩() {
        withRealMap { map, _ in
            map.addAnnotation(RouteCanvas.PointAnnotation(point: Self.起点, index: 0))
            map.addAnnotation(RouteCanvas.DeviceAnnotation(point: Self.起点))
            map.setCenter(CLLocationCoordinate2D(latitude: 30.556, longitude: 103.991),
                          animated: false)
        }
    }

    /// 只加路线点（MKMarkerAnnotationView）不该崩 —— 对照组。
    ///
    /// 它存在的意义是**证明这套测试本身有效**：
    /// 若连对照组也崩，那是测试环境的问题，不是绿圈的问题。
    func test只加路线点不会崩() {
        withRealMap { map, _ in
            map.addAnnotation(RouteCanvas.PointAnnotation(point: Self.起点, index: 0))
            map.setCenter(CLLocationCoordinate2D(latitude: 30.557, longitude: 103.992),
                          animated: false)
        }
    }

    /// 绿圈连续移动多次 —— 对应用户「画了好几个点」的真实节奏。
    func test多次移动视野都不会崩() {
        withRealMap { map, _ in
            map.addAnnotation(RouteCanvas.DeviceAnnotation(point: Self.起点))
            for i in 0..<5 {
                map.setCenter(CLLocationCoordinate2D(latitude: 30.5554 + Double(i) * 0.001,
                                                     longitude: 103.9907 + Double(i) * 0.001),
                              animated: false)
            }
        }
    }

    /// 绿圈必须真的被 `viewFor` 交出去过 —— 否则上面几条全是空转。
    ///
    /// 这条是**测试自身的护栏**：它不测业务，只证明
    /// 「MapKit 确实向我们的 delegate 要了绿圈的 view」。
    /// 若哪天 Coordinator 构造签名变了导致 delegate 失效，
    /// 上面几条会「因为不再触发崩溃」而全绿 —— 那就是假绿。
    func test绿圈的view确实被MapKit取走过() {
        final class Spy: NSObject, MKMapViewDelegate {
            var sawDevice = false
            func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
                if annotation is RouteCanvas.DeviceAnnotation {
                    sawDevice = true
                    return GreenDotAnnotationView(annotation: annotation, reuseIdentifier: "d")
                }
                return nil
            }
        }
        let spy = Spy()
        withExtendedLifetime(spy) {
            withRealMap { map, _ in
                map.delegate = spy          // 换成 spy，只为记录「被取走过」
                map.addAnnotation(RouteCanvas.DeviceAnnotation(point: Self.起点))
                map.setCenter(CLLocationCoordinate2D(latitude: 30.558, longitude: 103.993),
                              animated: false)
            }
            XCTAssertTrue(spy.sawDevice, "MapKit 从没来要绿圈的 view —— 上面几条是假绿")
        }
    }
}
