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
        WindowGroup {
            ContentView()
                .environmentObject(backend)
                .environmentObject(updateChecker)
                .onAppear {
                    appDelegate.backend = backend
                    backend.requestNotificationAuthorization()
                    updateChecker.startPolling()
                }
        }
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Photome 대시보드 열기") {
                    backend.openDashboard()
                }
                .disabled(!backend.isRunning)

                Button("백엔드 재시작") {
                    backend.restart()
                }
                .disabled(backend.isBusy)

                Divider()

                Button("전체 동기화 시작") {
                    backend.triggerLibraryScan()
                }
                .disabled(!backend.isRunning || backend.hasActiveLibraryJob)

                Button("이미지 AI 이어서 분석") {
                    backend.triggerSemanticMaintenance()
                }
                .disabled(!backend.isRunning || !backend.clipEnabled || backend.hasActiveLibraryJob)

                Divider()

                Button(backend.aiModeLabel) {
                    backend.toggleOfflineMode()
                }

                Button(backend.clipEnabled ? "이미지 AI 끄기" : "이미지 AI 켜기") {
                    backend.toggleClipEnabled()
                }

                Button("모델 캐시 폴더 열기") {
                    backend.openModelCache()
                }
            }
        }

        MenuBarExtra(backend.menuTitle, systemImage: menuBarIcon) {
            Button("Photome 열기") {
                NSApp.activate(ignoringOtherApps: true)
            }

            Button("대시보드 브라우저로 열기") {
                backend.openDashboard()
            }
            .disabled(!backend.isRunning)

            Divider()

            if let libraryJobStatus = backend.libraryJobStatus, backend.hasActiveLibraryJob {
                Text("현재 작업: \(libraryJobStatus.summary)")
            } else {
                Text("현재 작업: 대기 중")
            }

            if let aiPackStatus = backend.aiPackStatus, backend.clipEnabled {
                Text("이미지 AI: \(aiPackStatus.summary)")
            } else if !backend.clipEnabled {
                Text("이미지 AI: 꺼짐")
            } else {
                Text("이미지 AI: 상태 확인 중")
            }

            Divider()

            Button("전체 동기화 시작") {
                backend.triggerLibraryScan()
            }
            .disabled(!backend.isRunning || backend.hasActiveLibraryJob)

            Button("이미지 AI 이어서 분석") {
                backend.triggerSemanticMaintenance()
            }
            .disabled(!backend.isRunning || !backend.clipEnabled || backend.hasActiveLibraryJob)

            Button(backend.offlineMode ? "캐시만 로드" : "모델 준비") {
                backend.prepareAIModel(loadCached: backend.offlineMode)
            }
            .disabled(!backend.isRunning || !backend.clipEnabled || backend.aiPackStatus?.modelLoading == true)

            Button(backend.aiModeLabel) {
                backend.toggleOfflineMode()
            }

            Button(backend.clipEnabled ? "이미지 AI 끄기" : "이미지 AI 켜기") {
                backend.toggleClipEnabled()
            }

            Button("모델 캐시 폴더 열기") {
                backend.openModelCache()
            }

            Divider()

            Button("사진 폴더 선택") {
                backend.choosePhotoFolder()
            }

            Button(backend.lanEnabled ? "LAN 공유 끄기" : "LAN 공유 켜기") {
                backend.toggleLAN()
            }

            Button(launchAtLoginEnabled ? "로그인 시 자동 시작 끄기" : "로그인 시 자동 시작 켜기") {
                toggleLaunchAtLogin()
            }
            .disabled(!Self.isLaunchAtLoginAvailable())
            .help(Self.isLaunchAtLoginAvailable() ? "" : "이 옵션은 정식 .app 번들로 실행해야 사용 가능합니다.")

            Button("로그 보기") {
                backend.showLogs()
            }

            Button("진단 내보내기") {
                backend.exportDiagnosticsBundle()
            }

            Divider()

            if updateChecker.hasNewerRelease, let release = updateChecker.latestRelease {
                Button("새 버전 \(release.version) 다운로드…") {
                    updateChecker.openReleasePage()
                }
            }

            Button(updateChecker.isChecking ? "업데이트 확인 중..." : "업데이트 확인") {
                Task { await updateChecker.checkOnce() }
            }
            .disabled(updateChecker.isChecking)

            Divider()

            Button("백엔드 시작") {
                backend.start()
            }
            .disabled(backend.isRunning || backend.isBusy)

            Button("백엔드 재시작") {
                backend.restart()
            }
            .disabled(backend.isBusy)

            Button("백엔드 중지") {
                backend.stop()
            }
            .disabled(!backend.isRunning && !backend.isBusy)

            Divider()

            Text(backend.statusMessage)

            Button("종료") {
                backend.stop()
                NSApp.terminate(nil)
            }
        }
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
