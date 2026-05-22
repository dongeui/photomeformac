import SwiftUI

@main
struct PhotomeForMacApp: App {
    @StateObject private var backend = BackendSupervisor()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(backend)
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

            Button("사진 폴더 선택") {
                backend.choosePhotoFolder()
            }

            Button(backend.lanEnabled ? "LAN 공유 끄기" : "LAN 공유 켜기") {
                backend.toggleLAN()
            }

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

    private var menuBarIcon: String {
        switch backend.state {
        case .running:
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
