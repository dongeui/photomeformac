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
                Button("대시보드 열기") {
                    backend.openDashboard()
                }
                .disabled(!backend.isRunning)
            }
        }
    }
}
