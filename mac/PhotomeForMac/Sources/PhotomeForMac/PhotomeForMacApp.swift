import SwiftUI
import AppKit
import ServiceManagement

@main
struct PhotomeForMacApp: App {
    @StateObject private var backend = BackendSupervisor()
    @StateObject private var updateChecker = UpdateChecker()
    @State private var launchAtLoginEnabled = PhotomeForMacApp.currentLaunchAtLoginEnabled()
    @NSApplicationDelegateAdaptor(PhotomeAppDelegate.self) private var appDelegate

    /// 메뉴바 "Photome 열기"가 닫힌 메인 창을 다시 띄울 때 쓰는 식별자.
    static let mainWindowID = "photome-main"

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
        Window("Photome", id: Self.mainWindowID) {
            ContentView()
                .environmentObject(backend)
                .environmentObject(updateChecker)
                .onAppear {
                    appDelegate.backend = backend
                    // 알림 권한은 첫 실행 시점이 아니라 사용자가 첫 스캔/AI 작업을
                    // 시작할 때 요청한다(BackendSupervisor.trigger* 참고). 그래야 앱을
                    // 이해하기도 전에 권한 팝업이 뜨지 않는다.
                }
        }
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("Photome에 관하여") {
                    Self.presentAboutPanel()
                }
            }
            // 메뉴바 아이콘 메뉴는 상태 표시 전용으로 비웠으므로, 실제 조작·설정은
            // 여기(상단 앱 메뉴)와 앱 창 툴바에 둔다.
            CommandGroup(after: .appInfo) {
                Button("Photome 대시보드 열기") {
                    backend.openDashboard()
                }
                .disabled(!backend.isRunning)

                Button("업데이트 확인…") {
                    updateChecker.checkForUpdates()
                }
                .disabled(!updateChecker.canCheck)

                Button(launchAtLoginEnabled ? "로그인 시 자동 시작 끄기" : "로그인 시 자동 시작 켜기") {
                    toggleLaunchAtLogin()
                }
                .disabled(!Self.isLaunchAtLoginAvailable())

                Divider()

                Button("전체 동기화 시작") {
                    backend.triggerLibraryScan()
                }
                .disabled(!backend.isRunning || backend.hasActiveLibraryJob)

                Button("이미지 AI 이어서 분석") {
                    backend.triggerSemanticMaintenance()
                }
                .disabled(!backend.isRunning || !backend.clipEnabled || backend.hasActiveLibraryJob)

                Button("사진 폴더 선택") {
                    backend.choosePhotoFolder()
                }

                Button("Photome 다시 시작") {
                    backend.restart()
                }
                .disabled(backend.isBusy)

                Divider()

                Button("모델 준비/재로드") {
                    backend.prepareAIModel(loadCached: backend.offlineMode)
                }
                .disabled(!backend.isRunning || backend.aiPackStatus?.modelLoading == true)

                Button("모델 캐시 폴더 열기") {
                    backend.openModelCache()
                }

                Button("로그 보기") {
                    backend.showLogs()
                }

                Button("진단 내보내기") {
                    backend.exportDiagnosticsBundle()
                }
            }
        }

        MenuBarExtra(backend.menuTitle, systemImage: menuBarIcon) {
            MenuBarContent(backend: backend)
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

/// 메뉴바 아이콘 메뉴. 현재 상태를 보여주고, 동기화·이미지 AI의 시작/중지만
/// 컨트롤한다. 사진 보기/검색 등 나머지 조작은 "사진첩 열기"로 앱 창에서 한다.
/// 상태는 backend가 2초 폴링으로 갱신하며 메뉴를 열 때마다 최신값으로 평가된다.
struct MenuBarContent: View {
    @ObservedObject var backend: BackendSupervisor
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let activeKind = backend.hasActiveLibraryJob ? (backend.libraryJobStatus?.jobKind ?? "") : ""
        let scanRunning = activeKind == "scan"
        let aiRunning = activeKind == "semantic_maintenance" || activeKind == "semantic_backfill"

        Text("상태: \(backend.state.rawValue)")

        if let libraryJobStatus = backend.libraryJobStatus, backend.hasActiveLibraryJob {
            Text("현재 작업: \(libraryJobStatus.summary)")
        } else {
            Text("현재 작업: 대기 중")
        }

        if let aiPackStatus = backend.aiPackStatus {
            Text("이미지 AI: \(aiPackStatus.summary)")
        } else {
            Text("이미지 AI: 상태 확인 중")
        }

        if let coverage = backend.coverage {
            Text("사진 현황: \(coverage.summary)")
            if coverage.errors > 0 {
                Text("이미지 AI 오류 \(coverage.errors)건")
            }
        }

        Divider()

        if scanRunning {
            Button("동기화 중지") { backend.cancelActiveJob() }
        } else {
            Button("동기화 시작") { backend.triggerLibraryScan() }
                .disabled(!backend.isRunning || backend.hasActiveLibraryJob)
        }

        if aiRunning {
            Button("이미지 AI 분석 중지") { backend.cancelActiveJob() }
        } else {
            Button("이미지 AI 분석 시작") { backend.triggerSemanticMaintenance() }
                .disabled(!backend.isRunning || backend.hasActiveLibraryJob)
        }

        Divider()

        Button("사진첩 열기") {
            NSApp.activate(ignoringOtherApps: true)
            openWindow(id: PhotomeForMacApp.mainWindowID)
        }

        Button("종료") {
            backend.stop()
            NSApp.terminate(nil)
        }
    }
}

final class PhotomeAppDelegate: NSObject, NSApplicationDelegate {
    weak var backend: BackendSupervisor?

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
