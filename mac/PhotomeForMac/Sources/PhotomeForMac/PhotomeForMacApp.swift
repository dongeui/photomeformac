import SwiftUI
import AppKit
import ServiceManagement

@main
struct PhotomeForMacApp: App {
    @StateObject private var backend = BackendSupervisor()
    // 메뉴에서 수동 '업데이트 확인'은 없앴지만, 이 인스턴스가 살아 있어야
    // Sparkle 백그라운드 자동 확인(24h)이 돈다 — 제거하면 업데이트가 멈춘다.
    @StateObject private var updateChecker = UpdateChecker()
    @NSApplicationDelegateAdaptor(PhotomeAppDelegate.self) private var appDelegate

    /// SwiftPM의 bare executable로 실행하면 mainBundle이 .app이 아니어서
    /// `SMAppService.mainApp` 호출이 NSException(bundleProxyForCurrentProcess nil)을 던진다.
    /// Xcode ⌘R / `swift run` 같은 비-번들 실행에서도 앱이 죽지 않도록 가드한다.
    static func isLaunchAtLoginAvailable() -> Bool {
        Bundle.main.bundleURL.pathExtension == "app"
    }

    static func currentLaunchAtLoginEnabled() -> Bool {
        guard isLaunchAtLoginAvailable() else { return false }
        return SMAppService.mainApp.status == .enabled
    }

    var body: some Scene {
        // 창 없는 메뉴바 전용 앱. 사진첩/검색/설정은 "사진첩 열기"로 기본
        // 브라우저에서 열고, 네이티브 조작은 메뉴바 아이콘 메뉴에 모은다.
        MenuBarExtra(backend.menuTitle, systemImage: menuBarIcon) {
            MenuBarContent(
                backend: backend,
                // 시스템 설정에서 외부로 바꿔도 반영되도록, 고정값이 아니라
                // 메뉴가 다시 평가될 때마다 실제 상태를 읽는 클로저를 넘긴다.
                isLaunchAtLoginEnabled: { Self.currentLaunchAtLoginEnabled() },
                isLaunchAtLoginAvailable: Self.isLaunchAtLoginAvailable(),
                onToggleLaunchAtLogin: { toggleLaunchAtLogin() }
            )
            .onAppear { appDelegate.backend = backend }
        }
    }

    private func toggleLaunchAtLogin() {
        guard Self.isLaunchAtLoginAvailable() else {
            backend.updateStatusMessage(Localized.s("자동 시작은 .app 번들로 실행할 때만 사용 가능합니다."))
            return
        }
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
                backend.updateStatusMessage(Localized.s("로그인 시 자동 시작을 껐습니다."))
            } else {
                try SMAppService.mainApp.register()
                backend.updateStatusMessage(Localized.s("로그인 시 자동 시작을 켰습니다."))
            }
        } catch {
            backend.updateStatusMessage("로그인 자동 시작 변경 실패: \(error.localizedDescription)")
        }
    }

    private var menuBarIcon: String {
        switch backend.state {
        case .running:
            if backend.hasActiveLibraryJob {
                return "arrow.triangle.2.circlepath"
            }
            return "photo.stack.fill"
        case .starting, .stopping:
            return "arrow.triangle.2.circlepath"
        case .error:
            return "exclamationmark.triangle.fill"
        case .stopped:
            return "photo.stack"
        }
    }
}

/// 메뉴바 아이콘 메뉴. 상태·사진 현황·사진첩 열기 + 폴더 선택·설정·재시작·
/// 업데이트·자동 시작을 한 단계로 평면 배치한다(SwiftUI MenuBarExtra의 중첩
/// 서브메뉴는 hover 시 포커스가 메인으로 튀는 버그가 있어 Menu를 쓰지 않는다).
/// 동기화 설정은 웹의 "설정" 탭으로 일원화했다. 상태는 backend가
/// 2초 폴링으로 갱신하며 메뉴를 열 때마다 최신값으로 평가된다.
struct MenuBarContent: View {
    @ObservedObject var backend: BackendSupervisor
    let isLaunchAtLoginEnabled: () -> Bool
    let isLaunchAtLoginAvailable: Bool
    let onToggleLaunchAtLogin: () -> Void

    var body: some View {
        Text("\(Localized.s("상태")): \(backend.state.displayLabel)")

        // 작업이 도는 동안 "무엇을 어디까지" 했는지 메뉴에서 바로 보여준다.
        // 이게 없으면 누적 현황(남음 N장)만 보여서 분석이 멈춘 것처럼 읽힌다.
        if let job = backend.libraryJobStatus, job.isRunning {
            Text("\(Localized.s("지금")): \(job.badgeTitle) · \(job.summary)")
        }

        if let coverage = backend.coverage {
            if let job = backend.libraryJobStatus, job.isRunning, job.jobKind == "scan", coverage.remaining > 0 {
                Text("\(Localized.s("사진 현황")): \(coverage.summary) · \(Localized.s("동기화 후 분석 계속"))")
            } else {
                Text("\(Localized.s("사진 현황")): \(coverage.summary)")
            }
        }

        if let usage = backend.resourceUsage {
            Text("\(Localized.s("리소스")): \(usage.summary)")
        }

        Divider()

        Button(Localized.s("사진첩 열기")) {
            backend.openGallery()
        }
        .disabled(!backend.isRunning)

        Divider()

        // 동기화는 자동으로 돈다(시작 직후 + 주기 + 폴더 변경/재시작). 수동 '지금
        // 동기화' 버튼은 없앴다 — 시작 직후 '실행 중'과 자동 동기화 시작 사이에
        // 잠깐 활성화됐다 비활성화되는 혼선의 주범이었고, 자동 경로와 중복이다.
        //
        // 폴더 변경·재시작은 백엔드를 내려 진행 중 동기화를 끊으므로, 전환 중
        // (isBusy=시작/중지)이나 동기화 중에는 같은 기준으로 잠근다. 단 첫 실행
        // (state=.stopped, 폴더 없음)에선 폴더 선택이 열려 있어야 하므로 isRunning을
        // 요구하지 않는다. 백엔드가 먹통이면 healthLoop이 state=.error + 잡 nil로
        // 만들어 둘 다 다시 활성화된다 — 복구 탈출구 유지.
        Button(Localized.s("사진 폴더 선택")) {
            backend.choosePhotoFolder()
        }
        .disabled(backend.isBusy || backend.hasActiveLibraryJob)
        Button(Localized.s("설정 열기")) {
            backend.openDashboard()
        }
        .disabled(!backend.isRunning)
        Button(Localized.s("Trove 다시 시작")) {
            backend.restart()
        }
        .disabled(backend.isBusy || backend.hasActiveLibraryJob)

        Divider()

        // 업데이트는 백그라운드 자동 확인(24h, UpdateChecker)에 맡기고, 수동
        // '업데이트 확인'과 'Photome에 관하여' 메뉴는 노출하지 않는다.
        // Toggle은 켜졌을 때 메뉴에 체크마크를 표시해 on/off 상태가 한눈에
        // 보인다. isOn getter가 매 평가마다 실제 상태를 읽으므로 시스템 설정
        // 에서 외부로 바꾼 경우에도 메뉴를 다시 열면 반영된다.
        Toggle(Localized.s("로그인 시 자동 시작"), isOn: Binding(
            get: { isLaunchAtLoginEnabled() },
            set: { _ in onToggleLaunchAtLogin() }
        ))
        .disabled(!isLaunchAtLoginAvailable)

        Divider()

        Button(Localized.s("종료")) {
            backend.stop()
            NSApp.terminate(nil)
        }
    }
}

final class PhotomeAppDelegate: NSObject, NSApplicationDelegate {
    weak var backend: BackendSupervisor?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 창 없는 메뉴바 전용 앱: Dock 아이콘과 화면 상단 메뉴 막대를 숨기고
        // 우측 상단 메뉴바 아이콘만 남긴다.
        NSApp.setActivationPolicy(.accessory)
        // 설치 후 첫 실행: 아직 언어를 고르지 않았으면 한 번 물어본다.
        // 선택값은 UserDefaults에 저장되고, 백엔드 env(TROVE_LOCALE)로도 전달돼
        // 웹 UI 기본 언어까지 맞춘다.
        promptForLanguageIfNeeded()
    }

    @MainActor
    private func promptForLanguageIfNeeded() {
        guard !Localized.isChosen else { return }
        let alert = NSAlert()
        alert.messageText = Localized.s("언어를 선택하세요")  // 시스템 추정 언어로 표기
        alert.informativeText = Localized.s("나중에 설정에서 바꿀 수 있습니다.")
        // 추정 언어를 기본(첫) 버튼으로 올려 한 번에 진행하기 쉽게.
        let ordered = Localized.supported.sorted { lhs, _ in lhs.code == Localized.systemSuggested }
        for option in ordered {
            alert.addButton(withTitle: option.label)
        }
        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        let index = response.rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
        let chosen = ordered.indices.contains(index) ? ordered[index].code : Localized.systemSuggested
        Localized.set(chosen)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let backend, backend.hasActiveLibraryJob else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = Localized.s("백그라운드 작업이 진행 중입니다")
        alert.informativeText = Localized.s("지금 종료하면 진행 중인 동기화가 중단됩니다. 계속 종료할까요?")
        alert.addButton(withTitle: Localized.s("종료"))
        alert.addButton(withTitle: Localized.s("취소"))
        alert.alertStyle = .warning
        let response = alert.runModal()
        return response == .alertFirstButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
