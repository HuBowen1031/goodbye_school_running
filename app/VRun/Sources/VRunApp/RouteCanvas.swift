import SwiftUI
import MapKit

// ══════════════════════════════════════════════════════════════════
//  RouteCanvas.swift —— MKMapView 的封装
//
//  钢笔工具的交互（草图）
//    「类似『钢笔』工具，不要求用户一笔画完，只要从起点开始连续画即可」
//    → 钢笔开启时，**点一下地图就加一个点**；可以停笔再续；
//      Esc 收笔；⌘Z 撤销。
//
//  绿圈 = iPhone 当前定位
//    草图要求「从 iPhone 读取当前位置，提示用户从此处开始绘制路线」。
//    这里用 MKMapView 自己的 showsUserLocation —— 因为注入的位置
//    **就是设备的位置**，Mac 上看到的就是注入结果，一箭双雕。
// ══════════════════════════════════════════════════════════════════

struct RouteCanvas: NSViewRepresentable {

    /// 钢笔是否激活。由草图右侧那根竖排工具栏控制。
    var isPenActive: Bool
    var route: RouteModel
    /// 点到地图时回调（带地图坐标）
    var onPick: (GeoPoint) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> MKMapView {
        let map = MKMapView()
        map.delegate = context.coordinator
        map.showsUserLocation = true
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

    func updateNSView(_ map: MKMapView, context: Context) {
        context.coordinator.parent = self

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
            // 画完后自动把整条路线框进视野
            if route.points.count >= 2, let ov = map.overlays.first {
                map.setVisibleMapRect(ov.boundingMapRect,
                                      edgePadding: NSEdgeInsets(top: 80, left: 60, bottom: 80, right: 60),
                                      animated: true)
            }
        }

        // iPhone 定位（绿圈）
        if let dev = route.deviceLocation,
           context.coordinator.lastDevice == nil || context.coordinator.lastDevice != dev {
            context.coordinator.lastDevice = dev
            map.removeAnnotations(map.annotations.filter { $0 is DeviceAnnotation })
            map.addAnnotation(DeviceAnnotation(point: dev))
        }

        context.coordinator.penEnabled = isPenActive
    }

    static func dismantleNSView(_ nsView: MKMapView, coordinator: Coordinator) {
        nsView.delegate = nil
    }

    // MARK: - Coordinator

    final class Coordinator: NSObject, MKMapViewDelegate {
        var parent: RouteCanvas
        var penEnabled = false
        var lastPointCount = -1
        var lastDevice: GeoPoint?

        init(_ parent: RouteCanvas) { self.parent = parent }

        func mapView(_ mapView: MKMapView, handle event: NSEvent?) -> Bool { false }

        /// 点击加点。钢笔关闭时**不拦截** —— 让地图正常平移/缩放。
        func mapView(_ mapView: MKMapView, mouseDown event: NSEvent) -> Bool {
            guard penEnabled else { return false }
            let point = mapView.convert(event.locationInWindow, toCoordinateFrom: mapView)
            parent.onPick(GeoPoint(lat: point.latitude, lon: point.longitude))
            return true
        }

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
                let v = mapView.dequeueReusableAnnotationView(withIdentifier: id)
                    as? MKAnnotationView ?? MKAnnotationView(annotation: a, reuseIdentifier: id)
                v.annotation = a
                v.frame = CGRect(x: 0, y: 0, width: 26, height: 26)
                // 草图里的绿圈。macOS 的 MKAnnotationView 没有 `.view` 属性，
                // 得自己加一个子视图。
                if v.subviews.isEmpty {
                    let dot = GreenDotView(frame: v.bounds)
                    dot.autoresizingMask = [.width, .height]
                    v.addSubview(dot)
                }
                v.canShowCallout = true
                v.rightCalloutAccessoryView = NSButton(title: "从此处起笔",
                                                       target: nil, action: nil)
                v.calloutOffset = CGPoint(x: 0, y: 18)
                return v

            case let a as PointAnnotation:
                let id = "pt"
                let v = mapView.dequeueReusableAnnotationView(withIdentifier: id)
                    as? MKMarkerAnnotationView ?? MKMarkerAnnotationView(annotation: a, reuseIdentifier: id)
                v.annotation = a
                v.glyphImage = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: nil)
                v.markerTintColor = a.index == 0 ? .systemGreen : .systemBlue
                v.canShowCallout = false
                v.displayPriority = .defaultLow
                return v

            default:
                return nil
            }
        }

        /// 地图移动结束后让「绿圈」保持在视野里 —— 用户需要一直看到起点在哪。
        func mapView(_ mapView: MKMapView, regionDidChangeAnimated animated: Bool) {
            guard let dev = parent.route.deviceLocation else { return }
            let c = CLLocationCoordinate2D(latitude: dev.lat, longitude: dev.lon)
            if !mapView.visibleMapRect.contains(MKMapPoint(c)) {
                mapView.setCenter(c, animated: true)
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
}

/// 草图里那个绿圈：绿心 + 白边 + 外发光。
///
/// 之所以自己画而不是用 `MKMarkerAnnotationView`：
/// marker 的形状是固定的，做不出「白描边 + 光晕」这一圈层次感，
/// 而起点必须在地图上**一眼可辨** —— 用户要靠它决定从哪儿起笔。
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
