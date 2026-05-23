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

            if let aiPackStatus = backend.aiPackStatus, backend.clipEnabled {
                Text(aiPackStatus.summary)
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(aiPackColor(for: aiPackStatus).opacity(0.16))
                    .foregroundStyle(aiPackColor(for: aiPackStatus))
                    .clipShape(Capsule())
            }

            Text(backend.statusMessage)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)

            Spacer()

            Button(backend.aiModeLabel) {
                backend.toggleOfflineMode()
            }

            Button(backend.clipEnabled ? "이미지 AI 켜짐" : "이미지 AI 꺼짐") {
                backend.toggleClipEnabled()
            }

            Button("모델 폴더") {
                backend.openModelCache()
            }

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

            if !backend.sourceRoots.isEmpty {
                VStack(spacing: 6) {
                    Text("선택된 폴더")
                        .font(.caption.weight(.semibold))
                    ForEach(backend.sourceRoots, id: \.self) { path in
                        Text(path)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .lineLimit(2)
                    }
                }
                .frame(maxWidth: .infinity)
            }

            aiPackPanel

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

    private var aiPackPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("이미지 AI")
                .font(.headline)

            Text(aiPackDescription)
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                Button(backend.offlineMode ? "캐시만 로드" : "모델 준비") {
                    backend.prepareAIModel(loadCached: backend.offlineMode)
                }
                .disabled(!backend.isRunning || !backend.clipEnabled || backend.aiPackStatus?.modelLoading == true)

                if !backend.offlineMode {
                    Button("캐시만 로드") {
                        backend.prepareAIModel(loadCached: true)
                    }
                    .disabled(!backend.isRunning || !backend.clipEnabled || backend.aiPackStatus?.modelLoading == true)
                }

                Button("모델 폴더 열기") {
                    backend.openModelCache()
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color.primary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 18))
    }

    private var aiPackDescription: String {
        guard backend.clipEnabled else {
            return "이미지 AI가 꺼져 있습니다. 기본 검색과 갤러리는 계속 동작합니다."
        }
        guard let aiPackStatus = backend.aiPackStatus else {
            return backend.offlineMode
                ? "오프라인 모드입니다. 기존 모델 캐시가 있으면 바로 로드할 수 있습니다."
                : "온라인 준비 모드입니다. 모델 다운로드를 시작할 수 있습니다."
        }
        return backend.offlineMode
            ? "\(aiPackStatus.summary) · 오프라인 모드"
            : "\(aiPackStatus.summary) · 온라인 준비 모드"
    }

    private var statusColor: Color {
        switch backend.state {
        case .running: return .green
        case .starting, .stopping: return .orange
        case .error: return .red
        case .stopped: return .gray
        }
    }

    private func aiPackColor(for status: BackendSupervisor.AIPackStatus) -> Color {
        switch status.stage {
        case "ready": return .green
        case "downloading": return .orange
        case "needs_download": return .blue
        case "needs_packages", "error": return .red
        default: return .gray
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
