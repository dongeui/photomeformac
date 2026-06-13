import Foundation
import AppKit
import Sparkle

/// Sparkle 2 기반 자동 업데이트 controller.
///
/// 사용자 첫 정식 릴리스부터 Sparkle-aware로 출시되도록 코드 측은 미리 통합한다.
/// 운영 측에서는:
/// 1. edDSA key 쌍을 생성 (`generate_keys` from Sparkle tools)해서 private은
///    안전한 곳에, public은 Info.plist의 `SUPublicEDKey`에 박는다.
/// 2. 정적 호스팅(GitHub Pages 등)에 `appcast.xml`을 둔다.
/// 3. 빌드 시 `generate_appcast` 도구로 새 DMG의 edDSA 서명을 부착하고
///    appcast.xml에 새 release entry를 추가한다.
/// 4. Info.plist의 `SUFeedURL`을 그 appcast.xml의 https URL로 설정.
///
/// 자세한 절차는 docs/mac/USER_TODO.md 의 Sparkle 섹션 참고.
@MainActor
final class UpdateChecker: ObservableObject {
    @Published private(set) var lastError: String?
    @Published private(set) var lastCheckedAt: Date?
    @Published private(set) var isChecking: Bool = false

    private let updater: SPUUpdater
    private let driver: SPUStandardUserDriver
    private let delegate: SparkleDelegate

    init(automaticallyChecks: Bool = true) {
        let hostBundle = Bundle.main
        let delegate = SparkleDelegate()
        let driver = SPUStandardUserDriver(hostBundle: hostBundle, delegate: nil)
        let updater = SPUUpdater(
            hostBundle: hostBundle,
            applicationBundle: hostBundle,
            userDriver: driver,
            delegate: delegate
        )
        updater.automaticallyChecksForUpdates = automaticallyChecks
        // 24시간마다 자동 체크. Sparkle은 그 사이 지나야 실제 네트워크 호출.
        updater.updateCheckInterval = 24 * 60 * 60
        updater.automaticallyDownloadsUpdates = false  // 사용자 동의 후 다운로드
        do {
            try updater.start()
        } catch {
            NSLog("Sparkle updater failed to start: \(error)")
        }
        self.updater = updater
        self.driver = driver
        self.delegate = delegate
    }

    /// 수동 업데이트 확인. 현재 메뉴에는 노출하지 않으며(자동 24h 확인에 일임),
    /// 향후 수동 트리거를 다시 붙일 때를 위한 공개 API로 남겨둔다. 이미 최신이면
    /// 그 다이얼로그까지 Sparkle이 표준 UI로 표시한다.
    func checkForUpdates() {
        guard !isChecking else { return }
        isChecking = true
        defer { isChecking = false }
        lastCheckedAt = Date()
        updater.checkForUpdates()
    }

    var canCheck: Bool {
        updater.canCheckForUpdates
    }
}

@MainActor
private final class SparkleDelegate: NSObject, SPUUpdaterDelegate {
    nonisolated func feedURLString(for updater: SPUUpdater) -> String? {
        // Info.plist의 SUFeedURL을 우선 사용. 추후 사용자 환경변수로도 override 가능.
        nil
    }
}
