import Foundation

/// 네이티브 UI(메뉴·알림·상태) 다국어. 웹 UI는 백엔드의 app/locales/*.json이
/// 담당하고, 여기는 맥 앱 셸 전용이다. 둘은 로케일 코드(ko/en)와 첫 실행 선택값
/// (UserDefaults TroveLocale + 백엔드로 넘기는 TROVE_LOCALE)만 공유한다.
///
/// 키는 한국어 원문이며 그게 곧 ko 폴백이다. 영어는 table["en"]에 둔다.
/// 향후 로케일 추가 시 table에 사전 하나만 더 넣으면 된다(코드 구조 변경 없음).
enum Localized {
    static let defaultsKey = "TroveLocale"
    static let supported: [(code: String, label: String)] = [("ko", "한국어"), ("en", "English")]

    /// 저장된 로케일. 없으면 시스템 언어로 추정, 그래도 미지원이면 ko.
    static var current: String {
        if let stored = UserDefaults.standard.string(forKey: defaultsKey),
           supported.contains(where: { $0.code == stored }) {
            return stored
        }
        return systemSuggested
    }

    static var isChosen: Bool {
        UserDefaults.standard.string(forKey: defaultsKey) != nil
    }

    static var systemSuggested: String {
        let pref = Locale.preferredLanguages.first ?? "ko"
        return pref.lowercased().hasPrefix("ko") ? "ko" : "en"
    }

    static func set(_ code: String) {
        UserDefaults.standard.set(code, forKey: defaultsKey)
    }

    /// 한국어 원문 key를 현재 로케일 문자열로. en에 없으면 key(=ko) 그대로.
    static func s(_ key: String) -> String {
        if current == "ko" { return key }
        return table[current]?[key] ?? key
    }

    private static let table: [String: [String: String]] = [
        "en": [
            // 메뉴
            "사진첩 열기": "Open Photos",
            "사진 폴더 선택": "Choose photo folder",
            "설정 열기": "Open settings",
            "Trove 다시 시작": "Restart Trove",
            "로그인 시 자동 시작": "Launch at login",
            "종료": "Quit",
            "취소": "Cancel",
            // 상태 라벨 접두사
            "상태": "Status",
            "지금": "Now",
            "사진 현황": "Photos",
            "리소스": "Resources",
            "동기화 후 분석 계속": "analysis continues after sync",
            // 종료 확인 다이얼로그
            "백그라운드 작업이 진행 중입니다": "A background task is running",
            "지금 종료하면 진행 중인 동기화가 중단됩니다. 계속 종료할까요?":
                "Quitting now will interrupt the running sync. Quit anyway?",
            // 자동 시작 토글 결과 메시지
            "자동 시작은 .app 번들로 실행할 때만 사용 가능합니다.":
                "Launch at login is only available when running as an .app bundle.",
            "로그인 시 자동 시작을 껐습니다.": "Turned off launch at login.",
            "로그인 시 자동 시작을 켰습니다.": "Turned on launch at login.",
            // 메뉴바 타이틀(상태별)
            "Trove 동기화 중": "Trove — syncing",
            "Trove 실행 중": "Trove — running",
            "Trove 시작 중": "Trove — starting",
            "Trove 중지 중": "Trove — stopping",
            "Trove 오류": "Trove — error",
            "Trove 중지됨": "Trove — stopped",
            "동기화": "Sync",
            // 상태 enum 표시값
            "중지됨": "stopped",
            "시작 중": "starting",
            "실행 중": "running",
            "중지 중": "stopping",
            "오류": "error",
            // 알림
            "Trove — 폴더 접근 불가": "Trove — folder unavailable",
            "Trove — 폴더 복구됨": "Trove — folder restored",
            "Trove 백엔드 재시작": "Trove backend restarted",
            "Trove 백엔드 오류": "Trove backend error",
            // 폴더 선택 패널
            "Trove 사진 폴더 선택": "Choose Trove photo folder",
            "읽기 전용으로 스캔할 사진 폴더를 선택하세요.": "Choose a photo folder to scan (read-only).",
            // 첫 실행 언어 선택
            "언어를 선택하세요": "Choose your language",
            "나중에 설정에서 바꿀 수 있습니다.": "You can change this later in settings.",
        ],
    ]
}
