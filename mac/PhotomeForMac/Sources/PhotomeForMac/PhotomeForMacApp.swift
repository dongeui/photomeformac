import SwiftUI
import AppKit
import ServiceManagement

@main
struct PhotomeForMacApp: App {
    @StateObject private var backend = BackendSupervisor()
    @StateObject private var updateChecker = UpdateChecker()
    @State private var launchAtLoginEnabled = PhotomeForMacApp.currentLaunchAtLoginEnabled()
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
                updateChecker: updateChecker,
                launchAtLoginEnabled: launchAtLoginEnabled,
                isLaunchAtLoginAvailable: Self.isLaunchAtLoginAvailable(),
                onToggleLaunchAtLogin: { toggleLaunchAtLogin() },
                onAbout: { Self.presentAboutPanel() }
            )
            .onAppear { appDelegate.backend = backend }
        }
    }

    static func presentAboutPanel() {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "?"
        let build = info["CFBundleVersion"] as? String ?? "?"
        let credits = NSAttributedString(
            string: "Docker 없이 실행되는 macOS용 Photome.\n로컬 사진 라이브러리, AI 검색, 사람·장소 태그.\n\nGitHub: https://github.com/dongeui/photomeformac",
            attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]
        )
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Photome",
            .applicationVersion: version,
            .version: "Build \(build)",
            .credits: credits,
            NSApplication.AboutPanelOptionKey(rawValue: "Copyright"): "© Photome",
        ])
    }

    private func toggleLaunchAtLogin() {
        guard Self.isLaunchAtLoginAvailable() else {
            backend.updateStatusMessage("자동 시작은 .app 번들로 실행할 때만 사용 가능합니다.")
            return
        }
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
                launchAtLoginEnabled = false
                backend.updateStatusMessage("로그인 시 자동 시작을 껐습니다.")
            } else {
                try SMAppService.mainApp.register()
                launchAtLoginEnabled = true
                backend.updateStatusMessage("로그인 시 자동 시작을 켰습니다.")
            }
        } catch {
            launchAtLoginEnabled = Self.currentLaunchAtLoginEnabled()
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
/// 동기화·이미지 AI 등 제어는 웹의 "설정" 탭으로 일원화했다. 상태는 backend가
/// 2초 폴링으로 갱신하며 메뉴를 열 때마다 최신값으로 평가된다.
struct MenuBarContent: View {
    @ObservedObject var backend: BackendSupervisor
    @ObservedObject var updateChecker: UpdateChecker
    let launchAtLoginEnabled: Bool
    let isLaunchAtLoginAvailable: Bool
    let onToggleLaunchAtLogin: () -> Void
    let onAbout: () -> Void

    var body: some View {
        Text("상태: \(backend.state.rawValue)")

        if let coverage = backend.coverage {
            Text("사진 현황: \(coverage.summary)")
        }

        if let usage = backend.resourceUsage {
            Text("리소스: \(usage.summary)")
        }

        Divider()

        Button("사진첩 열기") {
            backend.openGallery()
        }
        .disabled(!backend.isRunning)

        Divider()

        Button("사진 폴더 선택") {
            backend.choosePhotoFolder()
        }
        Button("설정 열기") {
            backend.openDashboard()
        }
        .disabled(!backend.isRunning)
        Button("Photome 다시 시작") {
            backend.restart()
        }
        .disabled(backend.isBusy)

        Divider()

        Button("업데이트 확인…") {
            updateChecker.checkForUpdates()
        }
        .disabled(!updateChecker.canCheck)
        Button(launchAtLoginEnabled ? "로그인 시 자동 시작 끄기" : "로그인 시 자동 시작 켜기") {
            onToggleLaunchAtLogin()
        }
        .disabled(!isLaunchAtLoginAvailable)
        Button("Photome에 관하여") {
            onAbout()
        }

        Divider()

        Button("종료") {
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
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let backend, backend.hasActiveLibraryJob else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "백그라운드 작업이 진행 중입니다"
        alert.informativeText = "지금 종료하면 진행 중인 동기화/이미지 AI 작업이 중단됩니다. 계속 종료할까요?"
        alert.addButton(withTitle: "종료")
        alert.addButton(withTitle: "취소")
        alert.alertStyle = .warning
        let response = alert.runModal()
        return response == .alertFirstButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
