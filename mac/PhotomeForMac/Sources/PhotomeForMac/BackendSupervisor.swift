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
            case "needs_download", "needs_packages":
                // 정식 배포는 weights/패키지를 항상 번들하므로 "설치/다운로드 필요"는
                // 사용자에게 노출하지 않는다. 첫 사용 전 로드 대기 상태일 뿐이다.
                return "준비 중"
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

    /// 전체 라이브러리 진행 현황(/status 의 semantic.coverage). 현재 작업
    /// 진행률(LibraryJobStatus)과 달리 "전체 N장 중 AI M장 완료"처럼 누적 상태를
    /// 나타낸다. 메뉴바에서 한눈에 보기 위한 요약 전용.
    struct LibraryCoverage {
        let eligible: Int
        /// 분석 완료 = CLIP 임베딩과 검색문서가 모두 최신인 사진 수
        /// (/status semantic.coverage.analyzed_current). 설정 페이지의
        /// "분석 완료/남은 분석"과 같은 수치라 두 화면이 항상 일치한다.
        let analyzedDone: Int
        let remaining: Int
        let errors: Int

        var analyzedPercent: Int {
            eligible > 0 ? Int((Double(analyzedDone) / Double(eligible) * 100).rounded()) : 100
        }

        var summary: String {
            if eligible == 0 { return "아직 분석할 사진이 없습니다" }
            if remaining == 0 { return "전체 \(eligible)장 · 모두 최신 ✓" }
            return "전체 \(eligible)장 · 분석 완료 \(analyzedDone) (\(analyzedPercent)%) · 남음 \(remaining)"
        }
    }

    private struct AIPackPrepareResponse: Decodable {
        let ok: Bool
        let message: String
    }

    /// 메뉴바에 보여줄 Photome 자체 리소스 사용량. Photome이 띄운 백엔드
    /// python 프로세스와 이 메뉴바 앱, 정확히 두 프로세스만 합산한다
    /// (Activity Monitor에서는 'Python'과 'PhotomeForMac' 두 항목으로 갈라져 보인다).
    struct ResourceUsage {
        /// Activity Monitor와 같은 코어당 % 합산이라 멀티코어 작업 중엔 100을 넘는다.
        let cpuPercent: Double
        let backendMemoryBytes: UInt64
        let appMemoryBytes: UInt64

        private static func formatMemory(_ bytes: UInt64) -> String {
            let gb = Double(bytes) / 1_073_741_824
            return gb >= 1
                ? String(format: "%.1fGB", gb)
                : String(format: "%.0fMB", Double(bytes) / 1_048_576)
        }

        var summary: String {
            let total = Self.formatMemory(backendMemoryBytes + appMemoryBytes)
            guard backendMemoryBytes > 0 else {
                return String(format: "CPU %.0f%% · 메모리 %@ (앱만, 백엔드 꺼짐)", cpuPercent, total)
            }
            return String(
                format: "CPU %.0f%% · 메모리 %@ (백엔드 %@ · 앱 %@)",
                cpuPercent,
                total,
                Self.formatMemory(backendMemoryBytes),
                Self.formatMemory(appMemoryBytes)
            )
        }
    }

    /// proc_pid_rusage 기반 프로세스별 샘플러. CPU%는 두 샘플 간 CPU 시간
    /// 차분이라 호출 간격(헬스 루프 2초)이 곧 측정 창이 된다.
    private struct ResourceSampler {
        private var lastCPUTicks: [Int32: UInt64] = [:]
        private var lastSampledAt: [Int32: UInt64] = [:]

        private static let timebase: mach_timebase_info_data_t = {
            var info = mach_timebase_info_data_t()
            mach_timebase_info(&info)
            return info
        }()

        mutating func sample(pid: Int32) -> (cpuPercent: Double, memoryBytes: UInt64)? {
            var info = rusage_info_current()
            let result = withUnsafeMutablePointer(to: &info) { pointer in
                pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                    proc_pid_rusage(pid, RUSAGE_INFO_CURRENT, $0)
                }
            }
            guard result == 0 else {
                lastCPUTicks[pid] = nil
                lastSampledAt[pid] = nil
                return nil
            }
            let now = mach_absolute_time()
            var cpuPercent = 0.0
            let cpuTicks = info.ri_user_time + info.ri_system_time
            if let previousTicks = lastCPUTicks[pid],
               let previousAt = lastSampledAt[pid],
               now > previousAt, cpuTicks >= previousTicks {
                let cpuNanos = Self.ticksToNanos(cpuTicks - previousTicks)
                let wallNanos = Self.ticksToNanos(now - previousAt)
                if wallNanos > 0 {
                    cpuPercent = Double(cpuNanos) / Double(wallNanos) * 100.0
                }
            }
            lastCPUTicks[pid] = cpuTicks
            lastSampledAt[pid] = now
            return (cpuPercent, info.ri_phys_footprint)
        }

        private static func ticksToNanos(_ ticks: UInt64) -> UInt64 {
            ticks * UInt64(timebase.numer) / UInt64(timebase.denom)
        }
    }

    @Published private(set) var state: State = .stopped
    @Published private(set) var statusMessage: String = "백엔드가 아직 실행되지 않았습니다."
    @Published private(set) var lastError: String?
    @Published private(set) var logFileURL: URL?
    @Published private(set) var sourceRoots: [String]
    @Published private(set) var aiPackStatus: AIPackStatus?
    @Published private(set) var libraryJobStatus: LibraryJobStatus?
    @Published private(set) var coverage: LibraryCoverage?
    @Published private(set) var resourceUsage: ResourceUsage?
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

    /// 사용자가 지정한(또는 기본) 시작 포트. 점유 시 여기서부터 위로 빈 포트를 찾는다.
    let basePort: Int
    /// 실제 백엔드가 바인딩하는 포트. start() 때 basePort부터 빈 포트를 탐색해 갱신된다.
    /// baseURL/dashboardURL/healthz 등 모든 파생 URL이 이 값을 따른다.
    private(set) var port: Int

    private var process: Process?
    private var resourceSampler = ResourceSampler()
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
    /// NAS/외장 마운트가 살아있는지 직전 polling 결과. 변경 감지에 사용.
    private var lastSourceRootAvailability: [String: Bool] = [:]
    @Published private(set) var unavailableSourceRoots: [String] = []

    private static let sourceRootsDefaultsKey = "PhotomeSourceRoots"
    private static let sourceRootBookmarksKey = "PhotomeSourceRootBookmarks"
    private static let lanEnabledDefaultsKey = "PhotomeLANEnabled"
    private static let maxCrashRestartAttempts: Int = 1

    init(port: Int = 8000) {
        self.basePort = port
        self.port = port
        self.sourceRoots = UserDefaults.standard.stringArray(forKey: Self.sourceRootsDefaultsKey) ?? []
        self.lanEnabled = UserDefaults.standard.bool(forKey: Self.lanEnabledDefaultsKey)
        self.sourceRootBookmarks = Self.loadBookmarks()
        self.resolveAndAccessBookmarks()
        // 메뉴바 전용 앱이라 띄울 창이 없으므로, 선택해둔 폴더가 있으면 앱 시작과
        // 함께 백엔드를 자동 기동한다(이전에는 메인 창 onAppear가 하던 역할).
        if !self.sourceRoots.isEmpty {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.state == .stopped else { return }
                self.start()
            }
        }
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

    /// NAS/외장 폴더가 마운트 해제되면 사용자에게 알린다. 백엔드는 graceful하게
    /// degrade하지만 사용자는 왜 검색 결과가 줄어드는지 알 길이 없다.
    private func checkSourceRootAvailability() {
        let fm = FileManager.default
        var unavailable: [String] = []
        var newAvail: [String: Bool] = [:]
        for path in sourceRoots {
            var isDir: ObjCBool = false
            let exists = fm.fileExists(atPath: path, isDirectory: &isDir) && isDir.boolValue
            newAvail[path] = exists
            if !exists { unavailable.append(path) }
            let previous = lastSourceRootAvailability[path]
            if previous == true && !exists {
                scheduleNotification(
                    title: "Photome — 폴더 접근 불가",
                    body: "'\((path as NSString).lastPathComponent)' 폴더에 접근할 수 없습니다. NAS 마운트가 해제됐는지 확인하세요."
                )
            } else if previous == false && exists {
                scheduleNotification(
                    title: "Photome — 폴더 복구됨",
                    body: "'\((path as NSString).lastPathComponent)' 폴더에 다시 접근할 수 있습니다."
                )
            }
        }
        lastSourceRootAvailability = newAvail
        unavailableSourceRoots = unavailable
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

    var galleryURL: URL {
        baseURL.appendingPathComponent("gallery")
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

        // basePort가 점유돼 있으면 위로 빈 포트를 찾아 자동 폴백한다. 사용자가
        // 터미널에서 직접 프로세스를 죽이지 않아도 되게 한다. 못 찾으면 basePort를
        // 그대로 쓰고(기존 동작), 충돌 시 portConflict 힌트가 backstop으로 뜬다.
        let resolvedPort = Self.firstAvailablePort(startingAt: basePort, maxTries: 20) ?? basePort
        if resolvedPort != port {
            port = resolvedPort
        }
        if resolvedPort != basePort {
            statusMessage = "포트 \(basePort)이 사용 중이라 \(resolvedPort) 포트로 시작합니다."
        }

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

    /// 127.0.0.1:port에 TCP 소켓을 bind해 보고 성공하면 비어 있는 포트로 본다.
    /// 활성 LISTEN 중인 포트는 EADDRINUSE로 실패 → 점유로 판정. SO_REUSEADDR로
    /// TIME_WAIT 잔여 소켓은 비어 있는 것으로 취급한다(백엔드도 곧장 bind 가능).
    static func isPortAvailable(_ port: Int) -> Bool {
        guard port > 0, port <= 65535 else { return false }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        if fd < 0 { return false }
        defer { close(fd) }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return bound == 0
    }

    /// startingAt부터 위로 maxTries개 포트를 훑어 처음 비어 있는 포트를 돌려준다.
    static func firstAvailablePort(startingAt start: Int, maxTries: Int) -> Int? {
        var candidate = start
        var tries = 0
        while tries < maxTries && candidate <= 65535 {
            if isPortAvailable(candidate) { return candidate }
            candidate += 1
            tries += 1
        }
        return nil
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
                return "포트 \(port) 부근이 모두 사용 중입니다. [재시작]을 누르면 다시 빈 포트를 찾습니다. 계속되면 터미널에서 `lsof -tiTCP:\(port) -sTCP:LISTEN`로 점유 프로세스를 확인하세요."
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

    /// 사진첩(/gallery)을 기본 브라우저에서 연다. 앱 내장 WKWebView는 WebContent
    /// 프로세스가 ad-hoc/hardened 서명 환경에서 불안정해 입력·fetch가 막히므로,
    /// 상호작용이 필요한 웹 UI는 기본 브라우저로 띄운다.
    func openGallery() {
        NSWorkspace.shared.open(galleryURL)
    }

    /// 메뉴바 "사람 정리…" — 얼굴↔이름 매핑 전용 페이지(/people/manage)를 연다.
    func openPeopleManager() {
        NSWorkspace.shared.open(baseURL.appendingPathComponent("people/manage"))
    }

    /// 진행 중인 동기화/이미지 AI 작업을 취소한다 (메뉴바 "중지").
    func cancelActiveJob() {
        guard let jobID = libraryJobStatus?.jobID, !jobID.isEmpty else {
            statusMessage = "취소할 작업이 없습니다."
            return
        }
        actionTask?.cancel()
        actionTask = Task { [weak self] in
            guard let self else { return }
            let result = await self.postJSON(endpoint: "scan/jobs/\(jobID)/cancel")
            await MainActor.run {
                self.statusMessage = result.ok ? "작업을 중지하는 중입니다." : result.message
            }
            await self.refreshLibraryJobStatus()
        }
    }

    func toggleLAN() {
        lanEnabled.toggle()
        if process != nil {
            restart()
        }
    }

    /// Apple Photos 라이브러리 패키지의 `originals` 디렉토리. 시스템 사용자의
    /// 대다수가 여기에 사진을 저장하므로 자동 감지해서 onboarding에서 우선 추천한다.
    /// 패키지 자체는 read-only 권장이며, photome scanner는 일반 폴더와 동일하게 walk.
    static func detectedPhotosLibraryURL() -> URL? {
        guard let pictures = FileManager.default.urls(for: .picturesDirectory, in: .userDomainMask).first else {
            return nil
        }
        let candidate = pictures
            .appendingPathComponent("Photos Library.photoslibrary", isDirectory: true)
            .appendingPathComponent("originals", isDirectory: true)
        return FileManager.default.fileExists(atPath: candidate.path) ? candidate : nil
    }

    var detectedPhotosLibrary: URL? {
        guard sourceRoots.allSatisfy({ !$0.contains("Photos Library.photoslibrary") }) else {
            return nil
        }
        return Self.detectedPhotosLibraryURL()
    }

    /// Apple Photos `originals`를 source root로 즉시 추가한다. NSOpenPanel을 띄워
    /// 사용자가 [열기] 한 번 누르면 user-selected 권한이 부여되고 bookmark까지 저장.
    func addApplePhotosLibrary() {
        guard let originals = Self.detectedPhotosLibraryURL() else {
            statusMessage = "Apple Photos 라이브러리를 찾지 못했습니다."
            return
        }
        let panel = NSOpenPanel()
        panel.title = "Apple Photos 라이브러리 추가"
        panel.message = "감지된 'Photos Library.photoslibrary/originals' 폴더를 read-only로 추가합니다."
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = false
        panel.treatsFilePackagesAsDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = originals.deletingLastPathComponent()
        panel.nameFieldStringValue = "originals"
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            Task { @MainActor in
                self?.appendSourceRoots([url])
            }
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
        // 작업 완료 알림을 보낼 수 있게, 사용자가 긴 작업을 처음 시작하는 이 시점에서
        // (첫 실행 직후가 아니라) 알림 권한을 요청한다. macOS는 한 번만 팝업한다.
        requestNotificationAuthorization()
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
        requestNotificationAuthorization()
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
                    await MainActor.run { self.checkSourceRootAvailability() }
                } else if firstSuccess {
                    await MainActor.run {
                        self.state = .error
                        self.statusMessage = "백엔드 응답이 끊겼습니다."
                        self.aiPackStatus = nil
                        self.libraryJobStatus = nil
                    }
                }
                await MainActor.run { self.sampleResourceUsage() }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    /// 백엔드 python + 이 앱의 CPU/메모리를 측정해 메뉴바 표시용으로 갱신한다.
    /// Photome 소유 프로세스 두 개만 본다 — 시스템 전체 사용량이 아니다.
    private func sampleResourceUsage() {
        let appSample = resourceSampler.sample(pid: ProcessInfo.processInfo.processIdentifier)
        let backendSample = process.flatMap { resourceSampler.sample(pid: $0.processIdentifier) }
        guard appSample != nil || backendSample != nil else {
            resourceUsage = nil
            return
        }
        resourceUsage = ResourceUsage(
            cpuPercent: (appSample?.cpuPercent ?? 0) + (backendSample?.cpuPercent ?? 0),
            backendMemoryBytes: backendSample?.memoryBytes ?? 0,
            appMemoryBytes: appSample?.memoryBytes ?? 0
        )
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
            let coverage = Self.parseCoverage(payload: decoded)
            await MainActor.run {
                let previous = self.libraryJobStatus
                self.libraryJobStatus = job
                self.coverage = coverage
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
        guard let payload else { return nil }
        if let jobs = payload["jobs"] as? [String: Any],
           let active = jobs["active_library_job"] as? [String: Any],
           let jobKind = active["job_kind"] as? String,
           let status = active["status"] as? String {
            return LibraryJobStatus(
                jobID: active["job_id"] as? String,
                jobKind: jobKind,
                status: status,
                summary: summarizeLibraryJob(active)
            )
        }
        // 스케줄러가 직접 돌리는 이미지 AI는 processing_jobs 행 없이
        // scheduler.background_task로만 보고된다 — 잡이 없으면 이를 폴백으로
        // 읽어 메뉴바 '지금:' 줄과 타이틀이 백그라운드 분석도 보여주게 한다.
        if let scheduler = payload["scheduler"] as? [String: Any],
           let kind = scheduler["background_task_kind"] as? String,
           (scheduler["background_task_state"] as? String) == "running" {
            return LibraryJobStatus(
                jobID: nil,
                jobKind: kind,
                status: "running",
                summary: (scheduler["background_task_message"] as? String) ?? "백그라운드 분석 중"
            )
        }
        return nil
    }

    static func parseCoverage(payload: [String: Any]?) -> LibraryCoverage? {
        guard
            let payload,
            let semantic = payload["semantic"] as? [String: Any],
            let cov = semantic["coverage"] as? [String: Any],
            let eligible = intValue(cov["eligible_media"])
        else {
            return nil
        }
        // 구버전 백엔드(analyzed_current 없음)와 잠깐 섞여 돌 수 있어 폴백을 둔다.
        let analyzed = intValue(cov["analyzed_current"]) ?? intValue(cov["clip_embeddings_current"]) ?? 0
        let remaining = intValue(cov["remaining_for_analysis"])
            ?? ((intValue(cov["remaining_for_clip"]) ?? 0) + (intValue(cov["remaining_for_search"]) ?? 0))
        return LibraryCoverage(
            eligible: eligible,
            analyzedDone: analyzed,
            remaining: remaining,
            errors: intValue(cov["semantic_job_errors"]) ?? 0
        )
    }

    static func summarizeLibraryJob(_ job: [String: Any]) -> String {
        let result = job["result"] as? [String: Any]
        let progress = result?["progress"] as? [String: Any]
        let kind = job["job_kind"] as? String ?? ""
        let startedAt = parseISO8601(job["started_at"] as? String)

        if kind == "scan" {
            let scan = progress?["scan"] as? [String: Any]
            if let total = intValue(scan?["total"]) {
                let current = intValue(scan?["current"]) ?? 0
                let found = intValue(progress?["files_found"]) ?? total
                let failed = intValue(scan?["failed"]) ?? 0
                let eta = formatETA(startedAt: startedAt, current: current, total: total)
                return "스캔 중 · \(current) / \(total) · 발견 \(found) · 실패 \(failed)\(eta)"
            }
            let processed = progress?["processed"] as? [String: Any]
            if let total = intValue(processed?["total"]) {
                let current = intValue(processed?["current"]) ?? 0
                let succeeded = intValue(processed?["succeeded"]) ?? 0
                let failed = intValue(processed?["failed"]) ?? 0
                let eta = formatETA(startedAt: startedAt, current: current, total: total)
                return "처리 중 · \(current) / \(total) · 완료 \(succeeded) · 실패 \(failed)\(eta)"
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

    static func parseISO8601(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        // 백엔드가 보내는 "2026-06-03T22:31:23.123456" 형태와 표준 ISO8601 둘 다 시도.
        let f1 = ISO8601DateFormatter()
        f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f1.date(from: raw) { return d }
        let f2 = ISO8601DateFormatter()
        f2.formatOptions = [.withInternetDateTime]
        if let d = f2.date(from: raw) { return d }
        // datetime.utcnow().isoformat() — TZ 없음
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        for pattern in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss"] {
            formatter.dateFormat = pattern
            if let d = formatter.date(from: raw) { return d }
        }
        return nil
    }

    static func formatETA(startedAt: Date?, current: Int, total: Int) -> String {
        guard let startedAt, current > 0, total > current else { return "" }
        let elapsed = Date().timeIntervalSince(startedAt)
        guard elapsed > 2 else { return "" }
        let perItem = elapsed / Double(current)
        let remaining = Double(total - current) * perItem
        if remaining >= 3600 {
            let hours = Int(remaining / 3600)
            let mins = Int((remaining.truncatingRemainder(dividingBy: 3600)) / 60)
            return " · 약 \(hours)시간 \(mins)분 남음"
        }
        if remaining >= 90 {
            return " · 약 \(Int(remaining / 60))분 남음"
        }
        if remaining >= 10 {
            return " · 약 \(Int(remaining))초 남음"
        }
        return " · 곧 완료"
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

    private static let logMaxBytes: UInt64 = 10 * 1024 * 1024  // 10 MB
    private static let logKeepRotations: Int = 3                // .log.1 ~ .log.3

    private static func prepareLogFile(appDataRoot: URL) throws -> URL {
        let logsDirectory = appDataRoot.appendingPathComponent("logs", isDirectory: true)
        try FileManager.default.createDirectory(at: logsDirectory, withIntermediateDirectories: true)
        let logFileURL = logsDirectory.appendingPathComponent("photome-backend.log")
        rotateLogIfNeeded(at: logFileURL)
        if !FileManager.default.fileExists(atPath: logFileURL.path) {
            FileManager.default.createFile(atPath: logFileURL.path, contents: Data())
        }
        return logFileURL
    }

    /// 10MB 넘으면 .log → .log.1, .log.1 → .log.2 ... 식으로 회전. 가장 오래된 것은 삭제.
    /// 백엔드가 며칠씩 켜져 있어도 로그 파일이 무한 누적되지 않도록 한다.
    private static func rotateLogIfNeeded(at url: URL) {
        let fm = FileManager.default
        guard fm.fileExists(atPath: url.path) else { return }
        let attrs = try? fm.attributesOfItem(atPath: url.path)
        let size = (attrs?[.size] as? NSNumber)?.uint64Value ?? 0
        guard size >= logMaxBytes else { return }
        // 가장 오래된 .log.N부터 삭제
        let oldest = url.appendingPathExtension("\(logKeepRotations)")
        try? fm.removeItem(at: oldest)
        // .log.(N-1) → .log.N 으로 이동
        for i in stride(from: logKeepRotations - 1, through: 1, by: -1) {
            let from = url.appendingPathExtension("\(i)")
            let to = url.appendingPathExtension("\(i + 1)")
            guard fm.fileExists(atPath: from.path) else { continue }
            try? fm.moveItem(at: from, to: to)
        }
        // 현재 .log → .log.1
        let firstRotation = url.appendingPathExtension("1")
        try? fm.moveItem(at: url, to: firstRotation)
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
