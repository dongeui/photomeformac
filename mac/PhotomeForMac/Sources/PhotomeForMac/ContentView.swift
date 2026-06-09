import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject private var backend: BackendSupervisor
    @State private var dropTargeted: Bool = false

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                if backend.isRunning {
                    runningView
                } else {
                    landing
                }

                if dropTargeted {
                    RoundedRectangle(cornerRadius: 18)
                        .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 3, dash: [8]))
                        .background(Color.accentColor.opacity(0.08))
                        .overlay(
                            VStack(spacing: 6) {
                                Image(systemName: "folder.badge.plus").font(.system(size: 36))
                                Text("원본 폴더로 추가").font(.headline)
                            }
                            .foregroundStyle(Color.accentColor)
                        )
                        .padding(24)
                        .allowsHitTesting(false)
                }
            }
            .onDrop(of: [UTType.fileURL], isTargeted: $dropTargeted) { providers in
                Task { await handleDroppedProviders(providers) }
                return true
            }
        }
        .frame(minWidth: 720, minHeight: 560)
        .onAppear {
            // 사용자가 선택해둔 폴더가 있을 때만 자동 시작한다. 폴더가 비어 있는
            // 첫 실행에서는 landing의 [사진 폴더 선택] 흐름이 끝나야 백엔드가
            // 의미를 갖는다.
            if backend.state == .stopped && !backend.sourceRoots.isEmpty {
                backend.start()
            }
        }
    }

    @MainActor
    private func handleDroppedProviders(_ providers: [NSItemProvider]) async {
        var urls: [URL] = []
        for provider in providers {
            guard provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) else { continue }
            if let url = await Self.loadFileURL(from: provider) {
                var isDir: ObjCBool = false
                if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue {
                    urls.append(url)
                }
            }
        }
        guard !urls.isEmpty else { return }
        backend.appendSourceRoots(urls)
    }

    private static func loadFileURL(from provider: NSItemProvider) async -> URL? {
        await withCheckedContinuation { (continuation: CheckedContinuation<URL?, Never>) in
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                if let data = item as? Data, let url = URL(dataRepresentation: data, relativeTo: nil) {
                    continuation.resume(returning: url)
                } else if let url = item as? URL {
                    continuation.resume(returning: url)
                } else {
                    continuation.resume(returning: nil)
                }
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

            if let libraryJobStatus = backend.libraryJobStatus, backend.hasActiveLibraryJob {
                Text("\(libraryJobStatus.badgeTitle) · \(libraryJobStatus.summary)")
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(Color.blue.opacity(0.14))
                    .foregroundStyle(.blue)
                    .clipShape(Capsule())
            }

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

            Button("전체 동기화") {
                backend.triggerLibraryScan()
            }
            .disabled(!backend.isRunning || backend.hasActiveLibraryJob)

            Button("이미지 AI 이어서") {
                backend.triggerSemanticMaintenance()
            }
            .disabled(!backend.isRunning || !backend.clipEnabled || backend.hasActiveLibraryJob)

            Button("모델 캐시 열기") {
                backend.openModelCache()
            }

            Button("사진 폴더 선택") {
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
        let hasFolders = !backend.sourceRoots.isEmpty

        return VStack(spacing: 18) {
            Image(systemName: "photo.on.rectangle.angled")
                .font(.system(size: 52))
                .foregroundStyle(.secondary)

            Text("Photome for Mac")
                .font(.largeTitle.bold())

            Text(hasFolders
                 ? "선택해둔 사진 폴더로 백엔드를 바로 시작할 수 있습니다."
                 : "사진 폴더를 선택하면 백엔드가 자동으로 스캔을 시작합니다.")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            if !backend.unavailableSourceRoots.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("⚠️ 마운트 해제된 폴더가 있습니다")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.orange)
                    ForEach(backend.unavailableSourceRoots, id: \.self) { path in
                        Text((path as NSString).lastPathComponent)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .help(path)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.orange.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }

            if let hint = backend.startupHint {
                VStack(alignment: .leading, spacing: 4) {
                    Text(hint.title)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.red)
                    Text(hint.detail)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .multilineTextAlignment(.leading)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.red.opacity(0.08))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }

            if let libraryJobStatus = backend.libraryJobStatus, backend.hasActiveLibraryJob {
                Text(libraryJobStatus.summary)
                    .font(.caption)
                    .foregroundStyle(.blue)
                    .multilineTextAlignment(.center)
            }

            if hasFolders {
                selectedFoldersPanel
            } else if backend.detectedPhotosLibrary != nil {
                applePhotosSuggestion
            }

            aiPackPanel
            quickActionsPanel

            HStack(spacing: 10) {
                if hasFolders {
                    Button("백엔드 시작") { backend.start() }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(backend.isBusy)
                    Button("폴더 추가") { backend.choosePhotoFolder() }
                        .controlSize(.large)
                } else if backend.detectedPhotosLibrary != nil {
                    // Apple Photos suggestion 카드가 위에 떴으니 main row는 secondary로.
                    Button("다른 폴더만 직접 선택") { backend.choosePhotoFolder() }
                        .controlSize(.large)
                } else {
                    Button("사진 폴더 선택") { backend.choosePhotoFolder() }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                }
            }

            VStack(spacing: 4) {
                Text("첫 분석은 사진 수에 따라 수십 분에서 수 시간이 걸릴 수 있습니다.")
                Text("창을 닫아도 메뉴바 아이콘이 계속 떠 있고, 백그라운드에서 진행됩니다.")
                Text("로그 보기·진단 내보내기는 메뉴바 아이콘에서 사용할 수 있습니다.")
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)
            .multilineTextAlignment(.center)
        }
        .padding(32)
        .frame(maxWidth: 700)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .shadow(radius: 20)
    }

    /// 백엔드 실행 중 메인 뷰. 앱 내장 WKWebView 대신 "사진첩 열기"로 기본
    /// 브라우저를 띄우고, 자주 쓰는 제어와 진행 현황만 네이티브로 보여준다.
    private var runningView: some View {
        VStack(spacing: 18) {
            Image(systemName: "photo.stack.fill")
                .font(.system(size: 48))
                .foregroundStyle(.tint)

            Text("Photome 실행 중")
                .font(.title.bold())

            if let coverage = backend.coverage {
                Text(coverage.summary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            if let libraryJobStatus = backend.libraryJobStatus, backend.hasActiveLibraryJob {
                Text(libraryJobStatus.summary)
                    .font(.caption)
                    .foregroundStyle(.blue)
                    .multilineTextAlignment(.center)
            }

            Button("사진첩 열기") {
                backend.openGallery()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!backend.isRunning)

            HStack(spacing: 10) {
                Button("전체 동기화") { backend.triggerLibraryScan() }
                    .disabled(backend.hasActiveLibraryJob)
                Button("이미지 AI 이어서") { backend.triggerSemanticMaintenance() }
                    .disabled(!backend.clipEnabled || backend.hasActiveLibraryJob)
                Button("폴더 추가") { backend.choosePhotoFolder() }
            }

            if !backend.sourceRoots.isEmpty {
                selectedFoldersPanel
            }

            Text("사진첩·검색·사람 정리는 기본 브라우저에서 열립니다.\n제어·로그·진단은 메뉴바 아이콘에서도 할 수 있습니다.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(32)
        .frame(maxWidth: 620)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .shadow(radius: 20)
    }

    private var applePhotosSuggestion: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "photo.stack.fill")
                    .foregroundStyle(.tint)
                Text("Apple Photos 라이브러리를 찾았습니다")
                    .font(.subheadline.weight(.semibold))
            }
            Text("시스템 사진앱이 저장한 'Photos Library.photoslibrary' 안의 사진들을 추가할 수 있습니다. 추가해도 사진앱 데이터는 수정되지 않습니다 (read-only 스캔).")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Button("Apple Photos 추가") {
                    backend.addApplePhotosLibrary()
                }
                .buttonStyle(.borderedProminent)
                Button("다른 폴더 선택") {
                    backend.choosePhotoFolder()
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var selectedFoldersPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("선택된 폴더")
                    .font(.subheadline.weight(.semibold))
                Text("\(backend.sourceRoots.count)개")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Text("드래그·드롭 또는 ‘폴더 추가’로 더 넣을 수 있습니다")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            VStack(spacing: 4) {
                ForEach(backend.sourceRoots, id: \.self) { path in
                    HStack(spacing: 8) {
                        Image(systemName: "folder.fill")
                            .foregroundStyle(.secondary)
                            .imageScale(.small)
                        VStack(alignment: .leading, spacing: 1) {
                            Text((path as NSString).lastPathComponent)
                                .font(.caption.weight(.semibold))
                                .lineLimit(1)
                            Text(path)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .help(path)
                        Button {
                            backend.removeSourceRoot(path)
                        } label: {
                            Image(systemName: "minus.circle")
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                        .help("이 폴더 제거")
                    }
                    .padding(.vertical, 4)
                    .padding(.horizontal, 8)
                    .background(Color.gray.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.gray.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var quickActionsPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("빠른 작업")
                .font(.headline)

            Text("메뉴바 없이도 여기서 전체 동기화와 이미지 AI 분석을 바로 시작할 수 있습니다.")
                .font(.caption)
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                Button("전체 동기화 시작") {
                    backend.triggerLibraryScan()
                }
                .disabled(!backend.isRunning || backend.hasActiveLibraryJob)

                Button("이미지 AI 이어서 분석") {
                    backend.triggerSemanticMaintenance()
                }
                .disabled(!backend.isRunning || !backend.clipEnabled || backend.hasActiveLibraryJob)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color.primary.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 18))
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
