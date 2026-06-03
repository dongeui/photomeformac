import Foundation
import AppKit
import UserNotifications

@MainActor
final class BackendSupervisor: ObservableObject {
    enum State: String {
        case stopped = "중지됨"
        case starting = "시작 중"
        case running = "실행 중"
        case stopping = "중지 중"
        case error = "오류"
    }

    struct AIPackProgress: Decodable {
        let bytesDownloaded: Int?
        let bytesEstimated: Int?
        let fraction: Double?

        enum CodingKeys: String, CodingKey {
            case bytesDownloaded = "bytes_downloaded"
            case bytesEstimated = "bytes_estimated"
            case fraction
        }

        private static func formatMB(_ bytes: Int) -> String {
            let mb = Double(bytes) / (1024.0 * 1024.0)
            return mb >= 100 ? String(format: "%.0fMB", mb) : String(format: "%.1fMB", mb)
        }

        var label: String? {
            guard let downloaded = bytesDownloaded, downloaded > 0 else { return nil }
            if let estimated = bytesEstimated, estimated > 0 {
                let percent = Int(min(100, max(0, (fraction ?? Double(downloaded) / Double(estimated)) * 100.0)))
                return "\(Self.formatMB(downloaded)) / ~\(Self.formatMB(estimated)) · \(percent)%"
            }
            return Self.formatMB(downloaded)
        }
    }

    struct AIPackStatus: Decodable {
        let stage: String
        let depsReady: Bool
        let modelReady: Bool
        let modelLoading: Bool
        let modelError: String?
        let config: [String: String]
        let progress: AIPackProgress?

        enum CodingKeys: String, CodingKey {
            case stage
            case depsReady = "deps_ready"
            case modelReady = "model_ready"
            case modelLoading = "model_loading"
            case modelError = "model_error"
            case config
            case progress
        }

        var summary: String {
            switch stage {
            case "ready":
                return "모델 준비 완료"
            case "downloading":
                if let detail = progress?.label {
                    return "모델 다운로드 중 (\(detail))"
                }
                return "모델 준비 중"
            case "needs_download":
                return "모델 다운로드 필요"
            case "needs_packages":
                return "local AI pack 설치 필요"
            case "error":
                return modelError.map { "오류: \($0)" } ?? "모델 준비 오류"
            default:
                return stage
            }
        }
    }

    struct LibraryJobStatus {
        let jobID: String?
        let jobKind: String
        let status: String
        let summary: String

        var isRunning: Bool {
            ["queued", "running"].contains(status)
        }

        var badgeTitle: String {
            switch jobKind {
            case "scan":
                return "동기화"
            case "semantic_backfill", "semantic_maintenance":
                return "이미지 AI"
            default:
                return "작업"
            }
        }
    }

    private struct AIPackPrepareResponse: Decodable {
        let ok: Bool
        let message: String
    }

    @Published private(set) var state: State = .stopped
    @Published private(set) var statusMessage: String = "백엔드가 아직 실행되지 않았습니다."
    @Published private(set) var lastError: String?
    @Published private(set) var logFileURL: URL?
    @Published private(set) var sourceRoots: [String]
    @Published private(set) var aiPackStatus: AIPackStatus?
    @Published private(set) var libraryJobStatus: LibraryJobStatus?
    @Published var lanEnabled: Bool {
        didSet {
            guard lanEnabled != oldValue else { return }
            UserDefaults.standard.set(lanEnabled, forKey: Self.lanEnabledDefaultsKey)
        }
    }
    /// CLIP 이미지 AI는 정식 배포에서 항상 켜진 상태로 동작한다 (DMG에 PyTorch +
    /// weights 동봉). 사용자에게 토글을 노출하지 않는다.
    let clipEnabled: Bool = true
    /// 외부 인터넷 다운로드는 정식 배포에서 항상 차단된다. 번들 weights + 사용자
    /// 데이터 캐시만 사용. 사용자에게 토글을 노출하지 않는다.
    let offlineMode: Bool = true

    let port: Int

    private var process: Process?
    private var healthTask: Task<Void, Never>?
    private var actionTask: Task<Void, Never>?
    private var outputPipe: Pipe?
    private var logHandle: FileHandle?
    private var crashRestartAttempts: Int = 0
    private var lastNotifiedJobID: String?
    /// Path → security-scoped bookmark data. Persisted so the user does not have
    /// to re-pick the same folders after each app restart (NSOpenPanel hands out
    /// transient permission; bookmarks make it durable across launches).
    private var sourceRootBookmarks: [String: Data] = [:]
    /// URLs that we have `startAccessingSecurityScopedResource()`-ed; we balance
    /// these with `stopAccessing...` on stop()/deinit so OS-side counters stay sane.
    private var activeSecurityURLs: [URL] = []

    private static let sourceRootsDefaultsKey = "PhotomeSourceRoots"
    private static let sourceRootBookmarksKey = "PhotomeSourceRootBookmarks"
    private static let lanEnabledDefaultsKey = "PhotomeLANEnabled"
    private static let maxCrashRestartAttempts: Int = 1

    init(port: Int = 8000) {
        self.port = port
        self.sourceRoots = UserDefaults.standard.stringArray(forKey: Self.sourceRootsDefaultsKey) ?? []
        self.lanEnabled = UserDefaults.standard.bool(forKey: Self.lanEnabledDefaultsKey)
        self.sourceRootBookmarks = Self.loadBookmarks()
        self.resolveAndAccessBookmarks()
    }

    deinit {
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        try? logHandle?.close()
        process?.terminate()
        healthTask?.cancel()
        actionTask?.cancel()
        for url in activeSecurityURLs {
            url.stopAccessingSecurityScopedResource()
        }
    }

    private static func loadBookmarks() -> [String: Data] {
        guard let dict = UserDefaults.standard.dictionary(forKey: Self.sourceRootBookmarksKey) else {
            return [:]
        }
        var out: [String: Data] = [:]
        for (key, value) in dict {
            if let data = value as? Data {
                out[key] = data
            }
        }
        return out
    }

    private func persistBookmarks() {
        UserDefaults.standard.set(sourceRootBookmarks, forKey: Self.sourceRootBookmarksKey)
    }

    private func resolveAndAccessBookmarks() {
        // Resolve stored bookmarks, claim security-scoped access so the Python
        // backend (spawned as a child process) inherits read access to the user's
        // chosen folders even after Mac app updates or first launches under
        // hardened runtime + sandbox-style permissions.
        var refreshedRoots: [String] = []
        var refreshedBookmarks: [String: Data] = [:]
        for path in sourceRoots {
            guard let data = sourceRootBookmarks[path] else {
                refreshedRoots.append(path)
                continue
            }
            var isStale = false
            do {
                let url = try URL(
                    resolvingBookmarkData: data,
                    options: [.withSecurityScope],
                    relativeTo: nil,
                    bookmarkDataIsStale: &isStale
                )
                if url.startAccessingSecurityScopedResource() {
                    activeSecurityURLs.append(url)
                    refreshedRoots.append(url.path)
                    if isStale,
                       let renewed = try? url.bookmarkData(
                            options: [.withSecurityScope],
                            includingResourceValuesForKeys: nil,
                            relativeTo: nil
                       ) {
                        refreshedBookmarks[url.path] = renewed
                    } else {
                        refreshedBookmarks[url.path] = data
                    }
                } else {
                    refreshedRoots.append(path)
                    refreshedBookmarks[path] = data
                }
            } catch {
                refreshedRoots.append(path)
            }
        }
        sourceRoots = refreshedRoots
        sourceRootBookmarks = refreshedBookmarks
        if refreshedRoots != UserDefaults.standard.stringArray(forKey: Self.sourceRootsDefaultsKey) {
            UserDefaults.standard.set(refreshedRoots, forKey: Self.sourceRootsDefaultsKey)
        }
        persistBookmarks()
    }

    private func storeBookmark(for url: URL) {
        do {
            let data = try url.bookmarkData(
                options: [.withSecurityScope],
                includingResourceValuesForKeys: nil,
                relativeTo: nil
            )
            sourceRootBookmarks[url.path] = data
            if url.startAccessingSecurityScopedResource() {
                activeSecurityURLs.append(url)
            }
            persistBookmarks()
        } catch {
            NSLog("storeBookmark failed for \(url.path): \(error)")
        }
    }

    private func dropBookmark(for path: String) {
        sourceRootBookmarks.removeValue(forKey: path)
        if let idx = activeSecurityURLs.firstIndex(where: { $0.path == path }) {
            activeSecurityURLs[idx].stopAccessingSecurityScopedResource()
            activeSecurityURLs.remove(at: idx)
        }
        persistBookmarks()
    }

    var isRunning: Bool {
        state == .running
    }

    var isBusy: Bool {
        state == .starting || state == .stopping
    }

    var hasActiveLibraryJob: Bool {
        libraryJobStatus?.isRunning == true
    }

    var baseURL: URL {
        URL(string: "http://127.0.0.1:\(port)")!
    }

    var dashboardURL: URL {
        baseURL.appendingPathComponent("dashboard")
    }

    var healthURL: URL {
        baseURL.appendingPathComponent("healthz")
    }

    var aiPackStatusURL: URL {
        baseURL.appendingPathComponent("ai-pack/status")
    }

    var statusURL: URL {
        baseURL.appendingPathComponent("status")
    }

    var modelCacheURL: URL {
        Self.defaultAppDataRoot().appendingPathComponent("models", isDirectory: true)
    }

    var menuTitle: String {
        if let libraryJobStatus, state == .running {
            switch libraryJobStatus.jobKind {
            case "scan":
                return "Photome 스캔 중"
            case "semantic_backfill", "semantic_maintenance":
                return "Photome 이미지 AI 중"
            default:
                return "Photome 작업 중"
            }
        }
        switch state {
        case .running: return "Photome 실행 중"
        case .starting: return "Photome 시작 중"
        case .stopping: return "Photome 중지 중"
        case .error: return "Photome 오류"
        case .stopped: return "Photome 중지됨"
        }
    }


    func updateStatusMessage(_ message: String) {
        statusMessage = message
    }

    func start() {
        guard state == .stopped || state == .error else { return }
        state = .starting
        lastError = nil
        statusMessage = "백엔드를 시작합니다."

        do {
            let repoRoot = try Self.findRepoRoot()
            let python = try Self.findPythonExecutable(repoRoot: repoRoot)
            let appDataRoot = Self.defaultAppDataRoot()
            try FileManager.default.createDirectory(at: appDataRoot, withIntermediateDirectories: true)
            Self.seedPreinstalledModels(appDataRoot: appDataRoot)
            let logFileURL = try Self.prepareLogFile(appDataRoot: appDataRoot)
            self.logFileURL = logFileURL

            let env = try Self.buildBackendEnv(
                repoRoot: repoRoot,
                python: python,
                appDataRoot: appDataRoot,
                port: port,
                lan: lanEnabled,
                clipEnabled: clipEnabled,
                offlineMode: offlineMode
            )

            let proc = Process()
            proc.executableURL = python
            proc.arguments = ["-c", "from app.main import main; main()"]
            proc.currentDirectoryURL = repoRoot
            proc.environment = env

            let pipe = Pipe()
            let logHandle = try FileHandle(forWritingTo: logFileURL)
            try logHandle.seekToEnd()
            try Self.appendLogBanner(to: logHandle, port: port, lanEnabled: lanEnabled)
            proc.standardOutput = pipe
            proc.standardError = pipe
            Self.attachLogStreaming(pipe: pipe, logHandle: logHandle)
            proc.terminationHandler = { [weak self] terminatedProc in
                Task { @MainActor [weak self] in
                    self?.handleProcessTermination(terminatedProc)
                }
            }

            try proc.run()
            process = proc
            outputPipe = pipe
            self.logHandle = logHandle
            statusMessage = "백엔드 실행 확인 중입니다."
            startHealthLoop()
        } catch {
            state = .error
            lastError = error.localizedDescription
            statusMessage = "백엔드 시작 실패: \(error.localizedDescription)"
        }
    }

    func stop() {
        guard process != nil || state == .running || state == .starting else {
            state = .stopped
            return
        }
        state = .stopping
        statusMessage = "백엔드를 중지합니다."
        healthTask?.cancel()
        healthTask = nil
        actionTask?.cancel()
        actionTask = nil
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        outputPipe = nil
        if let proc = process {
            proc.terminationHandler = nil
            proc.terminate()
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
        state = .stopped
        statusMessage = "백엔드가 중지되었습니다."
        aiPackStatus = nil
        libraryJobStatus = nil
        crashRestartAttempts = 0
        updateDockBadge()
    }

    private func handleProcessTermination(_ terminatedProc: Process) {
        guard process === terminatedProc else { return }
        guard state == .running || state == .starting else { return }
        let exitStatus = terminatedProc.terminationStatus
        process = nil
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        outputPipe = nil
        try? logHandle?.close()
        logHandle = nil
        healthTask?.cancel()
        healthTask = nil
        if crashRestartAttempts < Self.maxCrashRestartAttempts {
            crashRestartAttempts += 1
            state = .error
            lastError = "백엔드가 비정상 종료(코드 \(exitStatus))되어 재시작합니다."
            statusMessage = lastError ?? "백엔드 비정상 종료"
            scheduleNotification(title: "Photome 백엔드 재시작",
                                 body: "백엔드가 예기치 않게 종료되어 자동으로 다시 시작합니다.")
            start()
        } else {
            state = .error
            lastError = "백엔드가 반복적으로 비정상 종료됩니다(코드 \(exitStatus)). 로그를 확인하세요."
            statusMessage = lastError ?? "백엔드 반복 비정상 종료"
            scheduleNotification(title: "Photome 백엔드 오류",
                                 body: "자동 재시작에 실패했습니다. 메뉴에서 ‘로그 보기’로 원인을 확인하세요.")
        }
        updateDockBadge()
    }

    func appendSourceRoots(_ urls: [URL]) {
        let existingPaths = Set(sourceRoots)
        var newPaths: [String] = []
        for url in urls {
            let path = url.path
            guard !path.isEmpty, !existingPaths.contains(path) else { continue }
            storeBookmark(for: url)
            newPaths.append(path)
        }
        guard !newPaths.isEmpty else { return }
        sourceRoots = sourceRoots + newPaths
        UserDefaults.standard.set(sourceRoots, forKey: Self.sourceRootsDefaultsKey)
        statusMessage = "원본 폴더 \(newPaths.count)개를 추가했습니다."
        if process != nil {
            restart()
        } else if state == .stopped {
            // 첫 폴더 추가 후 사용자가 별도로 [백엔드 시작]을 또 누르지 않아도
            // 곧장 라이브러리가 뜨도록 자동 시작한다.
            start()
        }
    }

    func removeSourceRoot(_ path: String) {
        guard sourceRoots.contains(path) else { return }
        sourceRoots.removeAll { $0 == path }
        UserDefaults.standard.set(sourceRoots, forKey: Self.sourceRootsDefaultsKey)
        dropBookmark(for: path)
        statusMessage = sourceRoots.isEmpty
            ? "원본 폴더 목록이 비었습니다."
            : "원본 폴더에서 제거했습니다."
        if process != nil {
            restart()
        }
    }

    enum StartupHint: Equatable {
        case portConflict(Int)
        case pythonMissing
        case permissionIssue
        case generic(String)

        var title: String {
            switch self {
            case .portConflict(let port): return "포트 \(port) 충돌"
            case .pythonMissing: return "Python 런타임을 찾을 수 없음"
            case .permissionIssue: return "권한 문제"
            case .generic: return "시작 실패"
            }
        }

        var detail: String {
            switch self {
            case .portConflict(let port):
                return "다른 프로세스가 \(port) 포트를 사용 중입니다. 터미널에서 `lsof -tiTCP:\(port) -sTCP:LISTEN`로 확인하고 종료하세요."
            case .pythonMissing:
                return "앱이 실행할 Python을 찾지 못했습니다. 개발 모드라면 .venv가 있는지, 정식 빌드라면 bundled runtime이 들어있는지 확인하세요."
            case .permissionIssue:
                return "선택한 폴더에 macOS 권한이 없습니다. 시스템 설정 → 개인정보 보호 및 보안에서 권한을 확인하세요."
            case .generic(let message):
                return message
            }
        }
    }

    var startupHint: StartupHint? {
        guard let error = lastError, !error.isEmpty else { return nil }
        let lower = error.lowercased()
        if lower.contains("address already in use") || lower.contains("port") && lower.contains("use") {
            return .portConflict(port)
        }
        if lower.contains("python") && (lower.contains("not found") || lower.contains("찾을 수 없")) {
            return .pythonMissing
        }
        if lower.contains("permission") || lower.contains("권한") {
            return .permissionIssue
        }
        return .generic(error)
    }

    func restart() {
        stop()
        start()
    }

    func openDashboard() {
        NSWorkspace.shared.open(dashboardURL)
    }

    func toggleLAN() {
        lanEnabled.toggle()
        if process != nil {
            restart()
        }
    }

    func choosePhotoFolder() {
        let panel = NSOpenPanel()
        panel.title = "Photome 사진 폴더 선택"
        panel.message = "읽기 전용으로 스캔할 사진 폴더를 선택하세요."
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.begin { [weak self] response in
            guard response == .OK else { return }
            let urls = panel.urls
            Task { @MainActor in
                guard let self else { return }
                // Replace mode: drop existing bookmarks before claiming new ones.
                for existing in self.activeSecurityURLs {
                    existing.stopAccessingSecurityScopedResource()
                }
                self.activeSecurityURLs.removeAll()
                self.sourceRootBookmarks.removeAll()
                var paths: [String] = []
                for url in urls {
                    self.storeBookmark(for: url)
                    paths.append(url.path)
                }
                self.sourceRoots = paths
                self.statusMessage = "선택한 폴더: \(paths.joined(separator: ", "))"
                UserDefaults.standard.set(paths, forKey: Self.sourceRootsDefaultsKey)
                if self.process != nil {
                    self.restart()
                } else if self.state == .stopped {
                    // 첫 선택 후 따로 [백엔드 시작]을 누를 필요 없이 바로 시작.
                    self.start()
                }
            }
        }
    }

    func showLogs() {
        guard let logFileURL else {
            statusMessage = "아직 생성된 로그 파일이 없습니다."
            return
        }
        NSWorkspace.shared.open(logFileURL)
        statusMessage = "로그 파일을 엽니다. · \(logFileURL.path)"
    }

    func exportDiagnosticsBundle() {
        do {
            let exportURL = try Self.createDiagnosticsBundle(
                logFileURL: logFileURL,
                sourceRoots: sourceRoots,
                lanEnabled: lanEnabled,
                clipEnabled: clipEnabled,
                offlineMode: offlineMode,
                state: state,
                dashboardURL: dashboardURL,
                lastError: lastError,
                statusMessage: statusMessage
            )
            NSWorkspace.shared.activateFileViewerSelecting([exportURL])
            statusMessage = "진단 번들을 만들었습니다. · \(exportURL.path)"
        } catch {
            lastError = error.localizedDescription
            statusMessage = "진단 번들 생성 실패: \(error.localizedDescription)"
        }
    }

    func openModelCache() {
        let cacheURL = modelCacheURL
        try? FileManager.default.createDirectory(at: cacheURL, withIntermediateDirectories: true)
        NSWorkspace.shared.open(cacheURL)
        statusMessage = "모델 캐시 폴더를 엽니다. · \(cacheURL.path)"
    }

    func prepareAIModel(loadCached: Bool) {
        guard isRunning else {
            statusMessage = "먼저 백엔드를 실행하세요."
            return
        }
        guard clipEnabled else {
            statusMessage = "이미지 AI가 꺼져 있습니다."
            return
        }

        actionTask?.cancel()
        actionTask = Task { [weak self] in
            guard let self else { return }
            let endpoint = loadCached ? "ai-pack/prepare?load_cached=true" : "ai-pack/prepare"
            let result = await self.postJSON(endpoint: endpoint)
            await MainActor.run {
                self.statusMessage = result.message
                if !result.ok {
                    self.lastError = result.message
                }
            }
            await self.refreshAIPackStatus()
            await self.refreshLibraryJobStatus()
        }
    }

    func triggerLibraryScan() {
        guard isRunning else {
            statusMessage = "먼저 백엔드를 실행하세요."
            return
        }
        actionTask?.cancel()
        actionTask = Task { [weak self] in
            guard let self else { return }
            let result = await self.postJSON(endpoint: "scan/async")
            await MainActor.run {
                self.statusMessage = result.ok ? "전체 동기화를 시작했습니다." : result.message
                if !result.ok {
                    self.lastError = result.message
                }
            }
            await self.refreshLibraryJobStatus()
        }
    }

    func triggerSemanticMaintenance() {
        guard isRunning else {
            statusMessage = "먼저 백엔드를 실행하세요."
            return
        }
        guard clipEnabled else {
            statusMessage = "이미지 AI가 꺼져 있습니다."
            return
        }
        actionTask?.cancel()
        actionTask = Task { [weak self] in
            guard let self else { return }
            let result = await self.postJSON(endpoint: "scan/semantic-maintenance/async")
            await MainActor.run {
                self.statusMessage = result.ok ? "이미지 AI 분석을 시작했습니다." : result.message
                if !result.ok {
                    self.lastError = result.message
                }
            }
            await self.refreshLibraryJobStatus()
        }
    }

    private func startHealthLoop() {
        healthTask?.cancel()
        healthTask = Task { [weak self] in
            guard let self else { return }
            var firstSuccess = false
            while !Task.isCancelled {
                let healthy = await self.probeHealth()
                if healthy {
                    firstSuccess = true
                    await MainActor.run {
                        self.state = .running
                        self.crashRestartAttempts = 0
                        self.statusMessage = self.lanEnabled
                            ? "실행 중 · LAN 공유 켜짐 · \(self.dashboardURL.absoluteString)"
                            : "실행 중 · 로컬 전용 · \(self.dashboardURL.absoluteString)"
                        self.updateDockBadge()
                    }
                    await self.refreshAIPackStatus()
                    await self.refreshLibraryJobStatus()
                } else if firstSuccess {
                    await MainActor.run {
                        self.state = .error
                        self.statusMessage = "백엔드 응답이 끊겼습니다."
                        self.aiPackStatus = nil
                        self.libraryJobStatus = nil
                    }
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func refreshAIPackStatus() async {
        do {
            let (data, response) = try await URLSession.shared.data(from: aiPackStatusURL)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            let status = try JSONDecoder().decode(AIPackStatus.self, from: data)
            await MainActor.run {
                self.aiPackStatus = status
            }
        } catch {
            await MainActor.run {
                self.aiPackStatus = nil
            }
        }
    }

    private func refreshLibraryJobStatus() async {
        do {
            let (data, response) = try await URLSession.shared.data(from: statusURL)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            let job = Self.parseLibraryJobStatus(payload: decoded)
            await MainActor.run {
                let previous = self.libraryJobStatus
                self.libraryJobStatus = job
                if let job, job.isRunning {
                    self.statusMessage = job.summary
                }
                self.handleJobTransition(previous: previous, current: job)
                self.updateDockBadge()
            }
        } catch {
            await MainActor.run {
                let previous = self.libraryJobStatus
                self.libraryJobStatus = nil
                self.handleJobTransition(previous: previous, current: nil)
                self.updateDockBadge()
            }
        }
    }

    private func handleJobTransition(previous: LibraryJobStatus?, current: LibraryJobStatus?) {
        guard let previous, previous.isRunning else { return }
        if let current, current.isRunning, current.jobID == previous.jobID { return }
        let key = "\(previous.jobID ?? "?")|\(previous.jobKind)"
        if key == lastNotifiedJobID { return }
        lastNotifiedJobID = key
        let succeeded: Bool
        if let current, !current.isRunning, current.jobID == previous.jobID {
            succeeded = current.status == "succeeded"
        } else {
            succeeded = true
        }
        let kindLabel: String
        switch previous.jobKind {
        case "scan": kindLabel = "사진 동기화"
        case "semantic_backfill", "semantic_maintenance": kindLabel = "이미지 AI"
        default: kindLabel = "작업"
        }
        let title = succeeded ? "\(kindLabel) 완료" : "\(kindLabel) 종료"
        let body = succeeded
            ? "백그라운드 \(kindLabel)가 끝났습니다."
            : "백그라운드 \(kindLabel)가 정상 종료되지 않았습니다. 대시보드에서 상태를 확인하세요."
        scheduleNotification(title: title, body: body)
    }

    private func updateDockBadge() {
        let badge: String
        if let job = libraryJobStatus, job.isRunning {
            switch job.jobKind {
            case "scan": badge = "스캔"
            case "semantic_backfill", "semantic_maintenance": badge = "AI"
            default: badge = "…"
            }
        } else if state == .error {
            badge = "!"
        } else {
            badge = ""
        }
        NSApp.dockTile.badgeLabel = badge.isEmpty ? nil : badge
    }

    private static func notificationsAvailable() -> Bool {
        Bundle.main.bundleURL.pathExtension == "app" && Bundle.main.bundleIdentifier != nil
    }

    func requestNotificationAuthorization() {
        guard Self.notificationsAvailable() else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func scheduleNotification(title: String, body: String) {
        guard Self.notificationsAvailable() else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    private func postJSON(endpoint: String) async -> (ok: Bool, message: String) {
        guard let url = URL(string: endpoint, relativeTo: baseURL) else {
            return (false, "잘못된 요청 경로")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let message = Self.extractMessage(from: payload)
            return (statusCode < 400, message)
        } catch {
            return (false, error.localizedDescription)
        }
    }

    private nonisolated func probeHealth() async -> Bool {
        do {
            let (_, response) = try await URLSession.shared.data(from: URL(string: "http://127.0.0.1:\(port)/healthz")!)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    private static func extractMessage(from payload: [String: Any]?) -> String {
        if let message = payload?["message"] as? String, !message.isEmpty {
            return message
        }
        if let detail = payload?["detail"] as? String, !detail.isEmpty {
            return detail
        }
        if let job = payload?["job"] as? [String: Any] {
            let jobKind = job["job_kind"] as? String ?? "job"
            let status = job["status"] as? String ?? "queued"
            let kindLabel: String
            switch jobKind {
            case "scan": kindLabel = "전체 동기화"
            case "semantic_backfill", "semantic_maintenance": kindLabel = "이미지 AI"
            default: kindLabel = jobKind
            }
            return "\(kindLabel) 작업이 \(status) 상태로 등록됐습니다."
        }
        return "요청을 처리했습니다."
    }

    static func parseLibraryJobStatus(payload: [String: Any]?) -> LibraryJobStatus? {
        guard
            let payload,
            let jobs = payload["jobs"] as? [String: Any],
            let active = jobs["active_library_job"] as? [String: Any],
            let jobKind = active["job_kind"] as? String,
            let status = active["status"] as? String
        else {
            return nil
        }
        return LibraryJobStatus(
            jobID: active["job_id"] as? String,
            jobKind: jobKind,
            status: status,
            summary: summarizeLibraryJob(active)
        )
    }

    static func summarizeLibraryJob(_ job: [String: Any]) -> String {
        let result = job["result"] as? [String: Any]
        let progress = result?["progress"] as? [String: Any]
        let kind = job["job_kind"] as? String ?? ""

        if kind == "scan" {
            let scan = progress?["scan"] as? [String: Any]
            if let total = intValue(scan?["total"]) {
                let current = intValue(scan?["current"]) ?? 0
                let found = intValue(progress?["files_found"]) ?? total
                let failed = intValue(scan?["failed"]) ?? 0
                return "스캔 중 · \(current) / \(total) · 발견 \(found) · 실패 \(failed)"
            }
            let processed = progress?["processed"] as? [String: Any]
            if let total = intValue(processed?["total"]) {
                let current = intValue(processed?["current"]) ?? 0
                let succeeded = intValue(processed?["succeeded"]) ?? 0
                let failed = intValue(processed?["failed"]) ?? 0
                return "처리 중 · \(current) / \(total) · 완료 \(succeeded) · 실패 \(failed)"
            }
            let summary = progress?["summary"] as? [String: Any]
            if let scanned = intValue(summary?["scanned"]) {
                let failed = intValue(summary?["failed"]) ?? 0
                return "스캔 중 · 발견 \(scanned) · 실패 \(failed)"
            }
            let stage = progress?["stage"] as? String
            let message = progress?["message"] as? String
            return "처리 중 · \(stage ?? message ?? "작업 중")"
        }

        let chunk = intValue(progress?["chunk"])
        let pending = intValue(progress?["pending"])
        let current = intValue(progress?["current"])
        let totalDone = intValue(progress?["total_succeeded"]) ?? intValue(progress?["succeeded"]) ?? 0
        let totalFailed = intValue(progress?["total_failed"]) ?? intValue(progress?["failed"]) ?? 0
        let totalEmbeddings = intValue(progress?["total_embeddings_created"]) ?? intValue(progress?["embeddings_created"]) ?? 0
        let totalTags = intValue(progress?["total_auto_tag_values"]) ?? intValue(progress?["auto_tag_values"]) ?? 0
        var parts = ["검색 분석 중"]
        if let chunk {
            parts.append("묶음 \(chunk)")
        }
        if pending != nil || current != nil {
            parts.append("\(current ?? 0) / \(pending ?? current ?? 0)")
        }
        parts.append("완료 \(totalDone)")
        parts.append("실패 \(totalFailed)")
        parts.append("AI +\(totalEmbeddings)")
        parts.append("태그 +\(totalTags)")
        return parts.joined(separator: " · ")
    }

    static func intValue(_ value: Any?) -> Int? {
        if let intValue = value as? Int {
            return intValue
        }
        if let doubleValue = value as? Double {
            return Int(doubleValue)
        }
        if let stringValue = value as? String, let intValue = Int(stringValue) {
            return intValue
        }
        return nil
    }

    private static func defaultAppDataRoot() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent("Photome", isDirectory: true)
    }

    /// 번들에 들어있는 CLIP weights를 사용자 데이터 폴더로 한 번 복사한다.
    /// 두 번째 실행부터는 이미 들어있어 빠르게 skip. 번들이 없거나 이미 캐시가 있으면 noop.
    private static func seedPreinstalledModels(appDataRoot: URL) {
        let userHub = appDataRoot
            .appendingPathComponent("models", isDirectory: true)
            .appendingPathComponent("huggingface", isDirectory: true)
            .appendingPathComponent("hub", isDirectory: true)
        let bundled = Bundle.main.resourceURL?
            .appendingPathComponent("preinstalled-models", isDirectory: true)
            .appendingPathComponent("huggingface", isDirectory: true)
            .appendingPathComponent("hub", isDirectory: true)
        guard let bundledHub = bundled,
              FileManager.default.fileExists(atPath: bundledHub.path) else { return }
        do {
            try FileManager.default.createDirectory(at: userHub, withIntermediateDirectories: true)
            let entries = try FileManager.default.contentsOfDirectory(at: bundledHub,
                                                                      includingPropertiesForKeys: nil)
            for entry in entries {
                let dest = userHub.appendingPathComponent(entry.lastPathComponent)
                if FileManager.default.fileExists(atPath: dest.path) { continue }
                try FileManager.default.copyItem(at: entry, to: dest)
            }
        } catch {
            // 복사 실패는 치명적이지 않다. 사용자가 첫 검색 시 다운로드 fallback 가능.
            NSLog("seedPreinstalledModels failed: \(error)")
        }
    }

    private static func createDiagnosticsBundle(
        logFileURL: URL?,
        sourceRoots: [String],
        lanEnabled: Bool,
        clipEnabled: Bool,
        offlineMode: Bool,
        state: State,
        dashboardURL: URL,
        lastError: String?,
        statusMessage: String
    ) throws -> URL {
        let appDataRoot = defaultAppDataRoot()
        let exportsDirectory = appDataRoot.appendingPathComponent("diagnostics", isDirectory: true)
        try FileManager.default.createDirectory(at: exportsDirectory, withIntermediateDirectories: true)

        let formatter = ISO8601DateFormatter()
        let safeStamp = formatter.string(from: Date()).replacingOccurrences(of: ":", with: "-")
        let bundleURL = exportsDirectory.appendingPathComponent("photome-diagnostics-\(safeStamp)", isDirectory: true)
        try FileManager.default.createDirectory(at: bundleURL, withIntermediateDirectories: true)

        if let logFileURL, FileManager.default.fileExists(atPath: logFileURL.path) {
            try FileManager.default.copyItem(at: logFileURL, to: bundleURL.appendingPathComponent("photome-backend.log"))
        }

        let payload: [String: Any] = [
            "created_at": formatter.string(from: Date()),
            "app_data_root": appDataRoot.path,
            "dashboard_url": dashboardURL.absoluteString,
            "state": state.rawValue,
            "lan_enabled": lanEnabled,
            "clip_enabled": clipEnabled,
            "offline_mode": offlineMode,
            "source_roots": sourceRoots,
            "last_error": lastError ?? "",
            "status_message": statusMessage,
            "macos_version": ProcessInfo.processInfo.operatingSystemVersionString
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: bundleURL.appendingPathComponent("diagnostics.json"), options: .atomic)
        return bundleURL
    }

    private static func prepareLogFile(appDataRoot: URL) throws -> URL {
        let logsDirectory = appDataRoot.appendingPathComponent("logs", isDirectory: true)
        try FileManager.default.createDirectory(at: logsDirectory, withIntermediateDirectories: true)
        let logFileURL = logsDirectory.appendingPathComponent("photome-backend.log")
        if !FileManager.default.fileExists(atPath: logFileURL.path) {
            FileManager.default.createFile(atPath: logFileURL.path, contents: Data())
        }
        return logFileURL
    }

    private static func appendLogBanner(to handle: FileHandle, port: Int, lanEnabled: Bool) throws {
        let formatter = ISO8601DateFormatter()
        let banner = "\n[\(formatter.string(from: Date()))] Photome backend start · port=\(port) · lan=\(lanEnabled ? "on" : "off")\n"
        if let data = banner.data(using: .utf8) {
            try handle.write(contentsOf: data)
        }
    }

    private static func attachLogStreaming(pipe: Pipe, logHandle: FileHandle) {
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            do {
                try logHandle.seekToEnd()
                try logHandle.write(contentsOf: data)
            } catch {
                handle.readabilityHandler = nil
            }
        }
    }

    private static func findRepoRoot() throws -> URL {
        let env = ProcessInfo.processInfo.environment
        if let explicit = env["PHOTOME_REPO_ROOT"], !explicit.isEmpty {
            return URL(fileURLWithPath: explicit, isDirectory: true)
        }

        var candidates: [URL] = []
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("photome-backend", isDirectory: true) {
            candidates.append(bundled)
        }
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true))
        candidates.append(URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photomeformac", isDirectory: true))

        for start in candidates {
            var current = start.standardizedFileURL
            for _ in 0..<8 {
                if FileManager.default.fileExists(atPath: current.appendingPathComponent("pyproject.toml").path),
                   FileManager.default.fileExists(atPath: current.appendingPathComponent("app/main.py").path) {
                    return current
                }
                current.deleteLastPathComponent()
            }
        }
        throw NSError(domain: "PhotomeForMac", code: 1, userInfo: [NSLocalizedDescriptionKey: "Photome repo root를 찾지 못했습니다."])
    }

    private static func findPythonExecutable(repoRoot: URL) throws -> URL {
        let env = ProcessInfo.processInfo.environment
        if let explicit = env["PHOTOME_PYTHON"], !explicit.isEmpty {
            return URL(fileURLWithPath: explicit)
        }

        let candidates = [
            repoRoot.appendingPathComponent(".venv/bin/python"),
            repoRoot.appendingPathComponent(".venv311/bin/python"),
            Bundle.main.resourceURL?.appendingPathComponent("python-runtime/bin/python"),
            Bundle.main.resourceURL?.appendingPathComponent("python-runtime/bin/python3"),
            URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photomeformac/.venv/bin/python"),
            URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photomeformac/.venv311/bin/python"),
            URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photome/.venv/bin/python"),
            URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photome/.venv311/bin/python"),
            URL(fileURLWithPath: "/usr/bin/python3")
        ].compactMap { $0 }

        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate.path) {
            return candidate
        }
        throw NSError(domain: "PhotomeForMac", code: 2, userInfo: [NSLocalizedDescriptionKey: "실행 가능한 Python을 찾지 못했습니다."])
    }

    private static func buildBackendEnv(
        repoRoot: URL,
        python: URL,
        appDataRoot: URL,
        port: Int,
        lan: Bool,
        clipEnabled: Bool,
        offlineMode: Bool
    ) throws -> [String: String] {
        let script = repoRoot.appendingPathComponent("scripts/mac_app_backend_env.py")
        let proc = Process()
        var arguments = [script.path, appDataRoot.path, "--port", String(port)]
        if lan {
            arguments.append("--lan")
        }
        if !clipEnabled {
            arguments.append("--no-clip")
        }
        if !offlineMode {
            arguments.append("--online")
        }
        proc.executableURL = python
        proc.arguments = arguments
        proc.currentDirectoryURL = repoRoot

        let output = Pipe()
        proc.standardOutput = output
        proc.standardError = output
        try proc.run()
        proc.waitUntilExit()

        guard proc.terminationStatus == 0 else {
            throw NSError(domain: "PhotomeForMac", code: 3, userInfo: [NSLocalizedDescriptionKey: "백엔드 env 생성 실패"])
        }

        let data = output.fileHandleForReading.readDataToEndOfFile()
        let decoded = try JSONDecoder().decode([String: String].self, from: data)
        var env = ProcessInfo.processInfo.environment
        env.merge(decoded) { _, new in new }
        env["PYTHONPATH"] = repoRoot.path
        env["PHOTOME_REPO_ROOT"] = repoRoot.path

        if let sourceRoots = UserDefaults.standard.stringArray(forKey: Self.sourceRootsDefaultsKey), !sourceRoots.isEmpty {
            env["PHOTOME_SOURCE_ROOTS"] = sourceRoots.joined(separator: ",")
        }
        return env
    }
}
