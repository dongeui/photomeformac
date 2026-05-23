import SwiftUI
import WebKit

struct ContentView: View {
    @EnvironmentObject private var backend: BackendSupervisor

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            ZStack {
                WebDashboardView(url: backend.dashboardURL, reloadToken: backend.state.rawValue)
                    .opacity(backend.isRunning ? 1 : 0.08)
                    .disabled(!backend.isRunning)

                if !backend.isRunning {
                    landing
                }
            }
        }
        .frame(minWidth: 1100, minHeight: 760)
        .onAppear {
            if backend.state == .stopped {
                backend.start()
            }
        }
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            Text("Photome")
                .font(.headline)

            Text(backend.state.rawValue)
                .font(.caption.weight(.semibold))
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(statusColor.opacity(0.16))
                .foregroundStyle(statusColor)
                .clipShape(Capsule())

            Text(backend.statusMessage)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Spacer()

            Button("사진 폴더") {
                backend.choosePhotoFolder()
            }

            Button(backend.lanEnabled ? "LAN 켜짐" : "로컬 전용") {
                backend.toggleLAN()
            }

            Button("브라우저") {
                backend.openDashboard()
            }
            .disabled(!backend.isRunning)

            Button("재시작") {
                backend.restart()
            }
            .disabled(backend.isBusy)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.regularMaterial)
    }

    private var landing: some View {
        VStack(spacing: 18) {
            Image(systemName: "photo.on.rectangle.angled")
                .font(.system(size: 52))
                .foregroundStyle(.secondary)

            Text("Photome for Mac")
                .font(.largeTitle.bold())

            Text("Docker 없이 기존 Photome 웹 UI와 백엔드를 Mac 앱 안에서 실행합니다.")
                .foregroundStyle(.secondary)

            if let error = backend.lastError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .textSelection(.enabled)
            }

            HStack {
                Button("백엔드 시작") {
                    backend.start()
                }
                .buttonStyle(.borderedProminent)
                .disabled(backend.isBusy)

                Button("사진 폴더 선택") {
                    backend.choosePhotoFolder()
                }

                Button("로그 보기") {
                    backend.showLogsPlaceholder()
                }
            }
        }
        .padding(32)
        .frame(maxWidth: 620)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .shadow(radius: 20)
    }

    private var statusColor: Color {
        switch backend.state {
        case .running: return .green
        case .starting, .stopping: return .orange
        case .error: return .red
        case .stopped: return .gray
        }
    }
}

struct WebDashboardView: NSViewRepresentable {
    let url: URL
    let reloadToken: String

    final class Coordinator {
        var lastReloadToken: String?
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    static func needsReload(currentURL: URL?, currentToken: String?, desiredURL: URL, desiredToken: String) -> Bool {
        guard currentURL?.absoluteString == desiredURL.absoluteString else {
            return true
        }
        return currentToken != desiredToken
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.allowsBackForwardNavigationGestures = true
        context.coordinator.lastReloadToken = reloadToken
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        guard Self.needsReload(
            currentURL: webView.url,
            currentToken: context.coordinator.lastReloadToken,
            desiredURL: url,
            desiredToken: reloadToken
        ) else {
            return
        }
        context.coordinator.lastReloadToken = reloadToken
        webView.load(URLRequest(url: url))
    }
}
