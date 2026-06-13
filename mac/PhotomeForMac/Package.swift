// swift-tools-version: 6.0
import PackageDescription

// 패키지 표시 이름은 Trove(브랜드). 실행 타깃·바이너리 이름은 PhotomeForMac을
// 유지한다 — 소스 폴더(Sources/PhotomeForMac)와 빌드 스크립트의 PRODUCT_NAME이
// 이 이름에 묶여 있어, 바꾸면 경로가 대량으로 깨진다. 사용자에게 보이는 .app
// 표시명은 빌드 스크립트가 CFBundleName=Trove로 따로 정한다.
let package = Package(
    name: "Trove",
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
        )
        // 테스트 타깃은 43ff060에서 Tests/ 삭제와 함께 제거됨. 디렉터리 없이
        // 선언만 남기면 SwiftPM이 Sources를 중복 소유로 보고 빌드가 깨진다.
    ]
)
