import Foundation
import AppKit

@MainActor
final class BackendSupervisor: ObservableObject {
    enum State: String {
        case stopped = "중지됨"
        case starting = "시작 중"
        case running = "실행 중"
        case stopping = "중지 중"
        case error = "오류"
    }

    @Published private(set) var state: State = .stopped
    @Published private(set) var statusMessage: String = "백엔드가 아직 실행되지 않았습니다."
    @Published private(set) var lastError: String?
    @Published private(set) var logFileURL: URL?
    @Published private(set) var sourceRoots: [String]
    @Published var lanEnabled: Bool {
        didSet {
            guard lanEnabled != oldValue else { return }
            UserDefaults.standard.set(lanEnabled, forKey: Self.lanEnabledDefaultsKey)
        }
    }

    let port: Int

    private var process: Process?
    private var healthTask: Task<Void, Never>?
    private var outputPipe: Pipe?
    private var logHandle: FileHandle?

    private static let sourceRootsDefaultsKey = "PhotomeSourceRoots"
    private static let lanEnabledDefaultsKey = "PhotomeLANEnabled"

    init(port: Int = 8000) {
        self.port = port
        self.sourceRoots = UserDefaults.standard.stringArray(forKey: Self.sourceRootsDefaultsKey) ?? []
        self.lanEnabled = UserDefaults.standard.bool(forKey: Self.lanEnabledDefaultsKey)
    }

    deinit {
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        try? logHandle?.close()
        process?.terminate()
        healthTask?.cancel()
    }

    var isRunning: Bool {
        state == .running
    }

    var isBusy: Bool {
        state == .starting || state == .stopping
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

    var menuTitle: String {
        switch state {
        case .running: return "Photome 실행 중"
        case .starting: return "Photome 시작 중"
        case .stopping: return "Photome 중지 중"
        case .error: return "Photome 오류"
        case .stopped: return "Photome 중지됨"
        }
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
            let logFileURL = try Self.prepareLogFile(appDataRoot: appDataRoot)
            self.logFileURL = logFileURL

            let env = try Self.buildBackendEnv(
                repoRoot: repoRoot,
                python: python,
                appDataRoot: appDataRoot,
                port: port,
                lan: lanEnabled
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
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        outputPipe = nil
        process?.terminate()
        process = nil
        try? logHandle?.close()
        logHandle = nil
        state = .stopped
        statusMessage = "백엔드가 중지되었습니다."
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
            let paths = panel.urls.map { $0.path }
            Task { @MainActor in
                self?.sourceRoots = paths
                self?.statusMessage = "선택한 폴더: \(paths.joined(separator: ", "))"
                UserDefaults.standard.set(paths, forKey: Self.sourceRootsDefaultsKey)
                if self?.process != nil {
                    self?.restart()
                }
            }
        }
    }

    func showLogsPlaceholder() {
        guard let logFileURL else {
            statusMessage = "아직 생성된 로그 파일이 없습니다."
            return
        }
        NSWorkspace.shared.open(logFileURL)
        statusMessage = "로그 파일을 엽니다. · \(logFileURL.path)"
    }

    private func startHealthLoop() {
        healthTask?.cancel()
        healthTask = Task { [weak self] in
            guard let self else { return }
            var firstSuccess = false
            while !Task.isCancelled {
                let healthy = await self.probeHealth()
                await MainActor.run {
                    if healthy {
                        firstSuccess = true
                        self.state = .running
                        self.statusMessage = self.lanEnabled
                            ? "실행 중 · LAN 공유 켜짐 · \(self.dashboardURL.absoluteString)"
                            : "실행 중 · 로컬 전용 · \(self.dashboardURL.absoluteString)"
                    } else if firstSuccess {
                        self.state = .error
                        self.statusMessage = "백엔드 응답이 끊겼습니다."
                    }
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
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

    private static func defaultAppDataRoot() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent("Photome", isDirectory: true)
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
            URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photome/.venv/bin/python"),
            URL(fileURLWithPath: "/Users/dongeui/Desktop/code/photome/.venv311/bin/python"),
            URL(fileURLWithPath: "/usr/bin/python3")
        ]

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
        lan: Bool
    ) throws -> [String: String] {
        let script = repoRoot.appendingPathComponent("scripts/mac_app_backend_env.py")
        let proc = Process()
        proc.executableURL = python
        proc.arguments = [script.path, appDataRoot.path, "--port", String(port)] + (lan ? ["--lan"] : [])
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
