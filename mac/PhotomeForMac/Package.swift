// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PhotomeForMac",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "PhotomeForMac", targets: ["PhotomeForMac"])
    ],
    targets: [
        .executableTarget(name: "PhotomeForMac")
    ]
)
