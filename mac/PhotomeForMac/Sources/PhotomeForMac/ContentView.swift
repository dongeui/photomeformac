import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var backend: BackendSupervisor

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Photome for Mac")
                .font(.largeTitle.bold())

            Text("Docker 없이 Photome 백엔드를 Mac 앱 내부 런타임으로 실행하기 위한 MVP shell입니다.")
                .foregroundStyle(.secondary)

            HStack {
                Text("상태")
                    .fontWeight(.semibold)
                Text(backend.state.rawValue)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(.quaternary)
                    .clipShape(Capsule())
            }

            Text(backend.statusMessage)
                .font(.callout)
                .foregroundStyle(.secondary)

            HStack {
                Button("백엔드 시작") {
                    backend.start()
                }
                .disabled(backend.isRunning)

                Button("대시보드 열기") {
                    backend.openDashboard()
                }
                .disabled(!backend.isRunning)

                Button("백엔드 중지") {
                    backend.stop()
                }
                .disabled(!backend.isRunning)
            }
        }
        .padding(24)
        .frame(minWidth: 520, minHeight: 260)
    }
}
