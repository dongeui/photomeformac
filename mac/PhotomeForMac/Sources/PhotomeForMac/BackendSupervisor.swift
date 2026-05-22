import Foundation
import AppKit

@MainActor
final class BackendSupervisor: ObservableObject {
    enum State: String {
        case stopped = "중지됨"
        case starting = "시작 중"
        case running = "실행 중"
        case error = "오류"
    }

    @Published private(set) var state: State = .stopped
    @Published private(set) var statusMessage: String = "백엔드가 아직 실행되지 않았습니다."

    private let dashboardURL = URL(string: "http://127.0.0.1:8000/dashboard")!

    var isRunning: Bool {
        state == .running
    }

    func start() {
        guard state == .stopped || state == .error else { return }
        state = .starting
        statusMessage = "MVP shell 준비됨. 다음 단계에서 Python 백엔드 프로세스 연결."
        state = .running
    }

    func stop() {
        state = .stopped
        statusMessage = "백엔드가 중지되었습니다."
    }

    func openDashboard() {
        NSWorkspace.shared.open(dashboardURL)
    }
}
