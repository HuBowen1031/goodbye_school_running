// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "VRun",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "VRunApp",
            path: "Sources/VRunApp",
            linkerSettings: [
                .linkedFramework("MapKit"),
                .linkedFramework("CoreLocation"),
            ]
        ),
        .testTarget(
            name: "VRunAppTests",
            dependencies: ["VRunApp"],
            path: "Tests/VRunAppTests"
        ),
    ]
)
