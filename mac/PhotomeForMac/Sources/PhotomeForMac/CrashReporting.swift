import Foundation
import Sentry

/// Opt-in 크래시 리포팅(Sentry). 동의는 이 Swift 셸이 소유하고, 백엔드(Python)는
/// `--crash-reporting --sentry-dsn`으로 env 게이트를 받는다(BackendSupervisor).
///
/// 설계 (변경 금지):
/// - **opt-in 전용, 기본 OFF.** DSN이 빌드에 주입(`TroveSentryDSN`)되고 사용자가
///   명시적으로 동의(`TroveCrashReportingConsent`)한 경우에만 `SentrySDK.start`.
/// - **크래시·예외만.** 성능 트레이싱/프로파일링·PII·breadcrumb는 모두 끈다.
/// - DSN이 비어 있으면(=개발 빌드) 메뉴 토글·동의 프롬프트 자체를 노출하지 않는다.
enum CrashReporting {
    /// 동의 여부(Bool). 사용자가 끄면 false.
    static let consentKey = "TroveCrashReportingConsent"
    /// 첫 실행 동의 프롬프트를 이미 한 번 보여줬는지. 결정(켜든 끄든)하면 true.
    static let decidedKey = "TroveCrashReportingDecided"
    /// 빌드 시 주입되는 DSN. 없으면 기능 전체가 비활성(개발 빌드).
    static let dsnInfoKey = "TroveSentryDSN"

    /// 빌드에 DSN이 주입돼 있어 크래시 리포팅을 쓸 수 있는가.
    static var isAvailable: Bool {
        !dsn.isEmpty
    }

    static var dsn: String {
        let value = Bundle.main.object(forInfoDictionaryKey: dsnInfoKey) as? String ?? ""
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 사용자가 동의했는가(기본 false).
    static var isEnabled: Bool {
        UserDefaults.standard.bool(forKey: consentKey)
    }

    /// 첫 실행 동의 프롬프트를 이미 거쳤는가.
    static var isDecided: Bool {
        UserDefaults.standard.bool(forKey: decidedKey)
    }

    static func setEnabled(_ enabled: Bool) {
        let defaults = UserDefaults.standard
        defaults.set(enabled, forKey: consentKey)
        defaults.set(true, forKey: decidedKey)
    }

    /// 동의 + DSN이 모두 있을 때만 Sentry를 시작한다. 그 외에는 no-op.
    static func startIfConsented() {
        guard isAvailable, isEnabled else { return }
        let dsn = self.dsn
        SentrySDK.start { options in
            options.dsn = dsn
            options.releaseName = "trove@\(appVersion)"
            // 크래시/예외만. 성능 트레이싱·프로파일링은 끈다.
            options.tracesSampleRate = 0.0
            // 사용자 콘텐츠·IP 등 PII 자동 수집 금지.
            options.sendDefaultPii = false
            options.maxBreadcrumbs = 0
            // 호스트명(serverName)에 사용자 계정명이 묻어 나갈 수 있어 비운다.
            options.beforeSend = { event in
                event.serverName = nil
                return event
            }
        }
    }

    static func stop() {
        SentrySDK.close()
    }

    private static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    }
}
