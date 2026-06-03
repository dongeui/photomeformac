// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PhotomeForMac",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "PhotomeForMac", targets: ["PhotomeForMac"])
    ],
    dependencies: [
        // Sparkle 2 — macOS 자동 업데이트 표준 프레임워크.
        // edDSA 서명된 appcast.xml을 GitHub Pages 같은 정적 호스팅에서 제공하면
        // 사용자가 클릭 한 번으로 새 DMG 다운로드 + 자동 교체 + 재시작까지 수행.
        .package(url: "https://github.com/sparkle-project/Sparkle.git", from: "2.6.0")
    ],
    targets: [
        .executableTarget(
            name: "PhotomeForMac",
            dependencies: [
                .product(name: "Sparkle", package: "Sparkle")
            ]
        ),
        .testTarget(name: "PhotomeForMacTests", dependencies: ["PhotomeForMac"])
    ]
)
