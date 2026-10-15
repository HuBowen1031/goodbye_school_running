import SwiftUI
import MapKit
import AppKit

/// 一次鼠标按下的阶段。抽出来是为了让点击判定能被单测直接驱动。
enum ClickPhase {
    case began, moved, ended, cancelled
}

// ══════════════════════════════════════════════════════════════════
//  PenMapView.swift —— 能吃掉「点击」的 MKMapView
//
//  ★ 为什么必须子类化（2026-10-14 实测挖出来的坑）
//
//      原实现把点击挂在 MKMapViewDelegate 上：
//          func mapView(_ mapView: MKMapView, mouseDown event: NSEvent) -> Bool
//
//      **这个方法根本不存在。** 查 MapKit 公开头文件
//      （MapKit.framework/Versions/A/Headers/MKMapView.h 的
//      @protocol MKMapViewDelegate，共约 30 个方法），
//      全框架搜 `mouseDown` / `mouseUp` / `handle:` —— **零命中**。
//
//      它能编译通过，是因为那只是一个普通的 NSObject 方法，
//      MapKit 永远不会调用它 —— 于是**点地图什么都不会发生**，
//      而钢笔按钮照样亮着、提示照样写着「点一下地图落一个点」。
//
//      症状极具迷惑性：界面上一切正常，只是「没反应」。
//
//      ⇒ 编译成功**不能**证明某个方法是委托方法。
//         接第三方框架的回调前，要么查 SDK 头文件，
//         要么用「变异测试」确认它真的被调用过。
//
//  ★ 为什么不能只覆盖 mouseDown
//      草图语义是「点一下加一个点」，**不是**「按下就加」。
//      只覆盖 mouseDown 的话，按下后随手一拖就会误加一个点。
//      所以要在 mouseUp 时才提交，且拖动超过阈值就作废。
// ══════════════════════════════════════════════════════════════════

/// 钢笔激活时吞掉点击的地图视图。
///
/// 钢笔**关闭**时全部走 `super`，地图的平移 / 缩放 / 滚轮行为**完全不变**。
final class PenMapView: MKMapView {

    /// 钢笔是否激活。为 nil 表示不接管。
    ///
    /// ⚠️⚠️ `didSet` **只在开关切换时**清理按压状态，闭包重建不算。
    ///
    ///     这是一个 2026-10-14 实测踩到的、**间歇性**丢点击的坑：
    ///
    ///         mouseDown  → pressOrigin = p
    ///             ↓ 期间 SwiftUI 重绘（estimate 的 250 ms 去抖就会触发）
    ///             ↓ updateNSView 重新赋值 penHandler
    ///             ↓ 若 didSet 无条件清理 → pressOrigin = nil
    ///         mouseUp    → guard pressOrigin != nil 失败 → **静默丢弃**
    ///
    ///     症状是「有时候点得中，有时候点不动」，**没有任何报错** ——
    ///     因为下一次 mouseDown 又会把 pressOrigin 填回来，看起来像随机失灵。
    ///
    ///     判定标准是**从 nil 变非 nil、或反过来**，而不是「有没有被赋值过」。
    var penHandler: ((CGPoint) -> Void)? {
        didSet {
            if (oldValue == nil) != (penHandler == nil) {
                // 钢笔开↔关的瞬间清掉半途按下的残留，
                // 否则「按下时开着、松开前关掉」会漏出一个点。
                pressOrigin = nil
                dragged = false
            }
        }
    }

    /// 拖动超过这个距离（pt）就判定为「在拖地图」，本次点击作废。
    private let dragSlop: CGFloat = 4
    private var pressOrigin: CGPoint?
    private var dragged = false

    /// ★ 把「按下 → 拖动 → 松开」的判定抽成纯逻辑，供单测直接驱动。
    ///
    /// 为什么要抽：这是钢笔唯一的交互入口，而 2026-10-14 才发现
    /// 它此前**根本没接上**（见文件头）。既然是新写的，就必须有测试钉住。
    /// 不抽的话只能靠真实鼠标事件测，CI 上跑不了 —— 而这类 bug
    /// 恰恰是「编译通过、界面正常、就是不响应」。
    @discardableResult
    func feedClickState(_ phase: ClickPhase, at point: CGPoint) -> Bool {
        switch phase {
        case .began:
            pressOrigin = point
            dragged = false
        case .moved:
            guard let o = pressOrigin else { return false }
            if hypot(point.x - o.x, point.y - o.y) > dragSlop { dragged = true }
        case .ended:
            defer {
                pressOrigin = nil
                dragged = false
            }
            guard pressOrigin != nil, !dragged else { return false }
            return true
        case .cancelled:
            pressOrigin = nil
            dragged = false
        }
        return false
    }

    override func mouseDown(with event: NSEvent) {
        guard penHandler != nil else {
            super.mouseDown(with: event)
            return
        }
        // ⚠️ **不调 super**。调了会把这次按下交给 MKMapView 的内部手势处理，
        //    于是既拖动了地图、又可能触发别的行为。
        //    钢笔激活时，这次点击完全归我们。
        _ = feedClickState(.began, at: convert(event.locationInWindow, from: nil))
    }

    override func mouseDragged(with event: NSEvent) {
        guard pressOrigin != nil else {
            super.mouseDragged(with: event)
            return
        }
        _ = feedClickState(.moved, at: convert(event.locationInWindow, from: nil))
    }

    override func mouseUp(with event: NSEvent) {
        guard pressOrigin != nil else {
            super.mouseUp(with: event)
            return
        }
        let point = convert(event.locationInWindow, from: nil)
        guard feedClickState(.ended, at: point), let handler = penHandler else { return }
        // 取 mouseUp 的位置而不是按下时的 —— 手指轻微抖动时落点更自然
        handler(point)
    }
}

// ══════════════════════════════════════════════════════════════════
//  RouteCanvas.swift —— MKMapView 的封装
//
//  钢笔工具的交互（草图）
//    「类似『钢笔』工具，不要求用户一笔画完，只要从起点开始连续画即可」
//    → 钢笔开启时，**点一下地图就加一个点**；可以停笔再续；
//      Esc 收笔；⌘Z 撤销。
//
//  绿圈 = 路线起点
//    草图要求「从 iPhone 读取当前位置，提示用户从此处开始绘制路线」。
//    实现上分两种情形（见 RouteModel.startAnchor(deviceConnected:)）：
//      · 设备已连接且报过位置 → 绿圈落在设备位置
//      · 未连接 / 位置未知  → 绿圈落在**用户点下的第一个点**
//    所以用户永远有一个起笔锚点，不会被「设备没插」卡住。
//
//  ⚠️ 关于 showsUserLocation：原稿在这里开了它，并注释说
//     「注入的位置就是设备的位置，Mac 上看到的就是注入结果」——
//     **这是错的**。macOS 的 MKMapView.showsUserLocation 显示的是
//     **这台 Mac 自己**的 CoreLocation，与 iPhone 被注入的坐标毫无关系，
//     而且还会触发 Mac 的定位授权弹窗。已移除。
//     iPhone 的位置只从 `device` 事件的 lat/lon 来，那才是唯一的真相源。
// ══════════════════════════════════════════════════════════════════

struct RouteCanvas: NSViewRepresentable {

    /// 钢笔是否激活。由草图右侧那根竖排工具栏控制。
    var isPenActive: Bool
    var route: RouteModel
    /// ★ 当前位置（会话进行中）。`nil` = 不画。
    ///
    /// 数据源是 `session.live.coordinate`，即**侧车真正注入进去的那个坐标**，
    /// 不是 Mac 自己的位置（那正是 `showsUserLocation` 的错误之处，见文件头）。
    var currentPosition: GeoPoint?
    /// 点到地图时回调（带地图坐标）
    var onPick: (GeoPoint) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> PenMapView {
        let map = PenMapView()
        map.delegate = context.coordinator
        // ⚠️ 不要开 showsUserLocation —— 它显示的是**这台 Mac**的位置，
        //    不是 iPhone 被注入的坐标，还会多弹一个 Mac 定位授权框。
        //    iPhone 的位置走 `device` 事件的 lat/lon（见文件头注释）。
        map.showsCompass = false
        map.pointOfInterestFilter = .excludingAll       // 少些干扰
        map.mapType = .standard

        // 初始视野：江安校区
        let center = CLLocationCoordinate2D(latitude: 30.5554, longitude: 103.9907)
        map.setRegion(MKCoordinateRegion(center: center,
                                        span: MKCoordinateSpan(latitudeDelta: 0.004,
                                                                longitudeDelta: 0.004)),
                      animated: false)
        return map
    }

    func updateNSView(_ map: PenMapView, context: Context) {
        context.coordinator.parent = self

        // ★ 钢笔接管点击。**不要**用 MKMapViewDelegate 的 mouseDown ——
        //   那个方法不存在，见 PenMapView 的文件头说明。
        //   这里每次 update 都重新挂一次闭包，保证捕获到最新的 onPick。
        map.penHandler = isPenActive
            ? { [weak map] viewPoint in
                guard let map else { return }
                let coord = map.convert(viewPoint, toCoordinateFrom: map)
                context.coordinator.parent.onPick(
                    GeoPoint(lat: coord.latitude, lon: coord.longitude))
            }
            : nil

        // 路线：只在点数变化时重建 overlay，避免每帧重画
        let sig = route.points.count
        if context.coordinator.lastPointCount != sig {
            context.coordinator.lastPointCount = sig
            map.removeOverlays(map.overlays)
            if route.points.count >= 2 {
                var coords = route.points.map { CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lon) }
                let poly = MKPolyline(coordinates: &coords, count: coords.count)
                map.addOverlay(poly)
            }
            map.removeAnnotations(map.annotations.filter { !($0 is MKUserLocation) })
            for (i, p) in route.points.enumerated() {
                map.addAnnotation(PointAnnotation(point: p, index: i))
            }
            // `animated: false` —— 但**理由和我当初写的不一样**，别照旧结论改回去。
            //
            // 当初以为「带动画 ⇒ MapKit 在定时器回调里重装配标注 ⇒ 与
            // SwiftUI 的下一轮 update 交错 ⇒ abort」，于是全改成无动画。
            // 2026-10-14 抓到真凶后重读了崩溃栈，发现 setCenter/setVisibleMapRect
            // **即使 animated: false 也是同步走完**的：
            //
            //     -[MKMapView setCenterCoordinate:animated:]
            //       → -[VKMapCameraController _animateToPosition:…]
            //         → -[MKMapView mapLayerDidChangeVisibleRegion]
            //           → -[MKAnnotationManager updateVisibleAnnotations]  ← 在这里抛
            //
            // 也就是说「定时器交错」这整套解释都是错的，
            // 无动画只是**顺手**绕开了另一条不稳定路径。
            //
            // 保持 animated: false 的真实理由：地图移动不必等动画走完，
            // 下一帧就是最终位置 —— 对一个「跟着点数重算视野」的画布来说更合适。
            // 真正的崩溃修法见 GreenDotAnnotationView 的文件头。
            if route.points.count >= 2, let ov = map.overlays.first {
                map.setVisibleMapRect(ov.boundingMapRect,
                                      edgePadding: NSEdgeInsets(top: 80, left: 60, bottom: 80, right: 60),
                                      animated: false)
            }
        }

        // 绿圈（路线起点）
        //
        // ⚠️ 未连接时绿圈与 `points[0]` **落在同一个坐标**。
        //    MKMapView 遇到坐标重合的多个 annotation 只会显示其中一个，
        //    所以绿圈必须后加（见下方顺序），并在 `viewFor` 里把
        //    `displayPriority` 提到 `.required` —— 否则会被路线点 marker 盖住，
        //    症状是「绿圈明明加了却看不见」。
        //    `anchorIsOnRoute` 用来判断是否重合，好把绿圈画大一点、
        //    让白描边不被 marker 吃掉。
        let anchor = route.startAnchor(deviceConnected: route.deviceConnected)
        context.coordinator.anchorIsOnRoute = anchor.map { a in
            route.points.first.map { abs($0.lat - a.lat) < 1e-9 && abs($0.lon - a.lon) < 1e-9 } ?? false
        } ?? false

        if let anchor, context.coordinator.lastAnchor != anchor {
            context.coordinator.lastAnchor = anchor
            context.coordinator.lastDevice = anchor
            map.removeAnnotations(map.annotations.filter { $0 is DeviceAnnotation })
            map.addAnnotation(DeviceAnnotation(point: anchor))
            // ★ 锚点**首次出现**时把视野带过去，让用户一眼看到起点在哪。
            //   只在这一刻做，不在每次地图移动时做 ——
            //   后者会和用户自己的平移打架：拖一下就被拽回去，非常难用。
            //
            // ⚠️ 崩溃就出在**这一行**：它是 2026-10-14 闪退的栈底第 19 帧，
            //   往上就是 `updateVisibleAnnotations` 装配绿圈 view 时抛异常。
            //   原因与 animation 无关，是 GreenDotAnnotationView 的问题，
            //   详见该类的文件头与 GreenDotMapKitTests.swift。
            if !context.coordinator.hasCenteredOnAnchor {
                context.coordinator.hasCenteredOnAnchor = true
                map.setCenter(CLLocationCoordinate2D(latitude: anchor.lat, longitude: anchor.lon),
                              animated: false)
            }
        } else if anchor == nil, context.coordinator.lastAnchor != nil {
            // 路线被清空 → 绿圈也要撤掉，否则会停在地图上冒充起点
            context.coordinator.lastAnchor = nil
            context.coordinator.hasCenteredOnAnchor = false
            map.removeAnnotations(map.annotations.filter { $0 is DeviceAnnotation })
        }

        // 当前位置（1 Hz）
        //
        // ⚠️ 只**改** annotation 的 coordinate，**绝不** remove + re-add。
        //
        //    理由是 F13 的教训：那次的崩溃就出在「移视野的同时增删 annotation」
        //    这条路径上。remove/add 会让 MapKit 整个销毁并重建 view，
        //    1 Hz 重建一次既浪费又在 MapKit 内部制造不必要的状态切换。
        //
        //    `coordinate` 标了 `@objc dynamic`，MapKit 会 KVO 到并自行挪动 view。
        if let cur = currentPosition {
            if let ann = context.coordinator.currentAnnotation {
                if Self.positionNeedsUpdate(from: ann.point, to: cur) {
                    ann.point = cur
                    ann.coordinate = CLLocationCoordinate2D(latitude: cur.lat, longitude: cur.lon)
                }
            } else {
                let ann = CurrentPositionAnnotation(point: cur)
                context.coordinator.currentAnnotation = ann
                map.addAnnotation(ann)
            }
        } else if context.coordinator.currentAnnotation != nil {
            context.coordinator.currentAnnotation = nil
            map.removeAnnotations(map.annotations.filter { $0 is CurrentPositionAnnotation })
        }
    }

    /// 当前位置挪得够不够远，值得动一下 annotation。
    ///
    /// 抽成静态纯函数是为了能单测 —— 它决定 1 Hz 下 MapKit 被打扰多少次。
    /// 阈值 1.5 m：10 km/h 时每秒走 2.8 m，跑步中每 tick 都会动；
    /// 停住时（预置阶段、暂停、到达终点）则完全不触发。
    static func positionNeedsUpdate(from old: GeoPoint, to new: GeoPoint) -> Bool {
        RouteModel.haversine(old, new) >= 1.5
    }

    static func dismantleNSView(_ nsView: PenMapView, coordinator: Coordinator) {
        nsView.delegate = nil
        nsView.penHandler = nil
    }

    // MARK: - Coordinator

    final class Coordinator: NSObject, MKMapViewDelegate {
        var parent: RouteCanvas
        var lastPointCount = -1
        /// 上一次绘制的绿圈位置（`nil` = 当前没有绿圈）。
        var lastAnchor: GeoPoint?
        /// 兼容旧字段，保留指向锚点的含义。
        var lastDevice: GeoPoint?
        /// 是否已经把视野带到过锚点上。只做一次。
        var hasCenteredOnAnchor = false
        /// 绿圈当前是否**压在路线第一个点**上 —— 决定要不要画白描边。
        var anchorIsOnRoute = false
        /// 当前位置的 annotation。会话期间**只挪坐标、不重建**（见 updateNSView）。
        var currentAnnotation: CurrentPositionAnnotation?

        init(_ parent: RouteCanvas) { self.parent = parent }

        // ⚠️ 这里曾有两个「看起来像委托」的方法：
        //      func mapView(_:handle:) -> Bool
        //      func mapView(_:mouseDown:) -> Bool
        //    两者都**不在 MKMapViewDelegate 协议里**，MapKit 永远不会调用，
        //    于是点地图毫无反应。已删除，改由 PenMapView 接管。
        //    见 PenMapView 的文件头说明。

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let poly = overlay as? MKPolyline {
                let r = MKPolylineRenderer(polyline: poly)
                r.strokeColor = NSColor.systemBlue
                r.lineWidth = 4
                r.lineCap = .round
                r.lineJoin = .round
                return r
            }
            return MKOverlayRenderer(overlay: overlay)
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            switch annotation {
            case let a as DeviceAnnotation:
                let id = "device"
                // ⚠️ 必须是 GreenDotAnnotationView（它覆盖了 clusteringIdentifier）。
                //    直接用基类 MKAnnotationView 会在加点时 abort 崩溃 ——
                //    详见该类的文档与 GreenDotMapKitTests.swift。
                let v: MKAnnotationView
                if let reused = mapView.dequeueReusableAnnotationView(withIdentifier: id)
                    as? GreenDotAnnotationView {
                    v = reused
                } else {
                    v = GreenDotAnnotationView(annotation: a, reuseIdentifier: id)
                }
                v.annotation = a
                // 压在路线点上时要更大一点，否则白描边会被 marker 吃掉
                let d: CGFloat = anchorIsOnRoute ? 34 : 26
                v.frame = CGRect(x: 0, y: 0, width: d, height: d)
                // macOS 的 MKAnnotationView 没有 `.view` 属性，得自己加一个子视图。
                // ⚠️ 复用时必须**整个换掉**：光改 frame 的话，上一种情形留下的
                //    子视图尺寸/内容会跟着流到下一种情形去（复用坑，实测踩过）。
                v.subviews.forEach { $0.removeFromSuperview() }
                let dot = GreenDotView(frame: v.bounds)
                dot.autoresizingMask = [.width, .height]
                v.addSubview(dot)
                // 绿圈是起点，必须压在路线点之上才看得见
                v.displayPriority = .required
                return v

            case let a as CurrentPositionAnnotation:
                let id = "here"
                let v = mapView.dequeueReusableAnnotationView(withIdentifier: id)
                    as? MKMarkerAnnotationView ?? MKMarkerAnnotationView(annotation: a, reuseIdentifier: id)
                v.annotation = a
                v.glyphImage = NSImage(systemSymbolName: "figure.run", accessibilityDescription: nil)
                v.markerTintColor = .systemOrange
                v.canShowCallout = false
                // 当前位置是会话进行中最该被看见的东西，不能被别的 marker 挤掉
                v.displayPriority = .required
                return v

            case let a as PointAnnotation:
                let id = "pt"
                let v = mapView.dequeueReusableAnnotationView(withIdentifier: id)
                    as? MKMarkerAnnotationView ?? MKMarkerAnnotationView(annotation: a, reuseIdentifier: id)
                v.annotation = a
                v.glyphImage = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
                // 起点标绿，其余标蓝 —— 与绿圈同色系，一眼能对上
                v.markerTintColor = a.index == 0 ? .systemGreen : .systemBlue
                v.canShowCallout = false
                v.displayPriority = a.index == 0 ? .defaultHigh : .defaultLow
                return v

            default:
                return nil
            }
        }
    }

    final class PointAnnotation: NSObject, MKAnnotation {
        let point: GeoPoint
        let index: Int
        var coordinate: CLLocationCoordinate2D {
            CLLocationCoordinate2D(latitude: point.lat, longitude: point.lon)
        }
        init(point: GeoPoint, index: Int) { self.point = point; self.index = index }
    }

    final class DeviceAnnotation: NSObject, MKAnnotation {
        let point: GeoPoint
        var coordinate: CLLocationCoordinate2D {
            CLLocationCoordinate2D(latitude: point.lat, longitude: point.lon)
        }
        init(point: GeoPoint) { self.point = point }
    }

    /// 当前位置。会话期间**只挪坐标、不重建 view**。
    ///
    /// ⚠️ `coordinate` 必须标 `@objc dynamic`（ObjC 侧叫 KVO-compliant）。
    ///    MKAnnotation 协议要求这个：MapKit 靠 KVO 观察坐标变化来移动 view。
    ///    漏了 `@objc dynamic` 的话编译照过、MapKit 照收，
    ///    只是**图上的点永远不动** —— 又是一个不报错的哑 bug。
    final class CurrentPositionAnnotation: NSObject, MKAnnotation {
        var point: GeoPoint
        @objc dynamic var coordinate: CLLocationCoordinate2D
        init(point: GeoPoint) {
            self.point = point
            self.coordinate = CLLocationCoordinate2D(latitude: point.lat, longitude: point.lon)
        }
    }
}

/// 绿圈的 annotation view。
///
/// ★ 这里的崩溃查了整整一轮，最后**不是**栈顶那个符号（2026-10-14 实测）
///
///   App 崩溃报告（asiBacktraces）逐帧是：
///
///       objc_exception_throw
///       -[MKAnnotationView clusteringIdentifier]          ← 栈顶看着像这里
///       -[MKMapView addAnnotationRepresentation:allowAnimation:]
///       -[MKAnnotationManager updateVisibleAnnotations]
///       -[MKMapView _didChangeRegionMidstream:]
///       -[MKMapView mapLayerDidChangeVisibleRegion]
///       VectorKit -[VKMapCameraController _animateToPosition:…]
///       -[MKMapView setCenterCoordinate:animated:]        ← updateNSView 里那一行
///       RouteCanvas.updateNSView(_:context:)
///
///   第一反应当然是「基类的 `clusteringIdentifier` 抽象，抛异常」，
///   于是去覆盖 getter。**但变异测试证明这个判断是错的**：
///   把 getter 覆盖删掉、只留 init 里的 `clusteringIdentifier = nil`，
///   5 条测试照样全绿 —— 赋值本身就等于「实现」了这个属性。
///
///   真正的凶手是靠一条「真把对象塞进 MKMapView」的测试才问出来的
///   （XCTest 会把 abort 变成可捕获的异常，于是拿到了 MapKit 的原话）：
///
///       <…DeviceAnnotation: 0x…> must implement title, or view must have a
///       non-nil detailCalloutAccessoryView when canShowCallout is YES
///       (NSGenericException)
///
///   ⇒ **`canShowCallout = true` 而 annotation 没有 title，MapKit 直接抛。**
///      绿圈本来也不需要 callout —— 之前挂的「从此处起笔」按钮
///      `target: nil, action: nil`，点了什么也不会发生，是个骗人的死按钮。
///
///   ⇒ 教训两条，写在这里免得再犯：
///      1. **崩溃栈顶的符号不等于真凶**。MapKit 在这个深度上的符号化不可靠，
///         拿它当结论就会一路修错地方 —— 必须拿到异常的 reason 才算数。
///      2. **纯逻辑测试证明不了「框架会接受这个对象」**。
///         钢笔那 26 条测试全绿、编译零警告、加点数据全对，
///         进程照样 abort —— 因为 abort 发生在 MapKit 内部，
///         纯逻辑测试根本没有能力覆盖那一层。
///         见 GreenDotMapKitTests.swift。
final class GreenDotAnnotationView: MKAnnotationView {

    override init(annotation: MKAnnotation?, reuseIdentifier: String?) {
        super.init(annotation: annotation, reuseIdentifier: reuseIdentifier)
        // `clusteringIdentifier` 是留给子类实现的属性，**赋值即实现**。
        // nil = 不参与聚类（绿圈只有一个，永远没东西可聚）。
        // ⚠️ 变异测试确认过：这里赋值就够了，不必再覆盖 getter ——
        //    早先「必须覆盖 getter」的判断是错的，别改回去。
        clusteringIdentifier = nil
        // ★ 不开 callout。开了又没 title，MapKit 会抛 NSGenericException
        //   （见文件头）。绿圈只是「起点」的视觉标记，本来也不需要交互。
        canShowCallout = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) 未使用")
    }
}

/// 草图里那个绿圈：绿心 + 白边 + 外发光。**它标的是「路线起点」**。
///
/// 之所以自己画而不是用 `MKMarkerAnnotationView`：
/// marker 的形状是固定的，做不出「白描边 + 光晕」这一圈层次感，
/// 而起点必须在地图上**一眼可辨** —— 用户要靠它决定从哪儿起笔。
///
/// ⚠️ 它的含义随设备状态而变（见 `RouteModel.startAnchor`）：
/// 连上手机时落在设备位置，没连时落在用户点下的第一个点。
/// 两种情形用**同一个视觉**，因为用户要回答的问题是同一个：
/// 「我从哪儿起笔」。
final class GreenDotView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let inset = bounds.insetBy(dx: 3, dy: 3)
        let glow = NSBezierPath(ovalIn: bounds)
        NSColor.systemGreen.withAlphaComponent(0.35).setFill()
        glow.fill()

        NSColor.white.setStroke()
        let ring = NSBezierPath(ovalIn: inset)
        ring.lineWidth = 3
        ring.stroke()

        NSColor.systemGreen.setFill()
        NSBezierPath(ovalIn: inset.insetBy(dx: 3, dy: 3)).fill()
    }
}
