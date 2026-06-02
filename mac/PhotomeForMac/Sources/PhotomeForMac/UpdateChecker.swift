import Foundation
import AppKit
import UserNotifications

@MainActor
final class UpdateChecker: ObservableObject {
    struct ReleaseInfo: Equatable {
        let version: String
        let tag: String
        let htmlURL: URL
        let publishedAt: String
    }

    @Published private(set) var latestRelease: ReleaseInfo?
    @Published private(set) var lastCheckedAt: Date?
    @Published private(set) var lastError: String?
    @Published private(set) var isChecking: Bool = false

    private let owner: String
    private let repo: String
    private let currentVersion: String
    private var pollTask: Task<Void, Never>?

    private static let lastSeenVersionKey = "PhotomeUpdateLastSeenVersion"
    private static let pollIntervalSeconds: TimeInterval = 6 * 60 * 60

    init(owner: String = "dongeui",
         repo: String = "photomeformac",
         currentVersion: String? = nil) {
        self.owner = owner
        self.repo = repo
        if let supplied = currentVersion, !supplied.isEmpty {
            self.currentVersion = supplied
        } else if let bundleVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String,
                  !bundleVersion.isEmpty {
            self.currentVersion = bundleVersion
        } else {
            self.currentVersion = "0.0.0"
        }
    }

    var hasNewerRelease: Bool {
        guard let latest = latestRelease else { return false }
        return Self.isVersion(latest.version, newerThan: currentVersion)
    }

    var releasesPageURL: URL {
        URL(string: "https://github.com/\(owner)/\(repo)/releases/latest")!
    }

    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.checkOnce()
                try? await Task.sleep(nanoseconds: UInt64(Self.pollIntervalSeconds * 1_000_000_000))
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func checkOnce() async {
        guard !isChecking else { return }
        isChecking = true
        defer { isChecking = false }

        let endpoint = URL(string: "https://api.github.com/repos/\(owner)/\(repo)/releases/latest")!
        var request = URLRequest(url: endpoint)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("photomeformac-update-checker", forHTTPHeaderField: "User-Agent")
        request.timeoutInterval = 12

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw URLError(.badServerResponse)
            }
            if http.statusCode == 404 {
                lastError = nil
                lastCheckedAt = Date()
                return
            }
            guard (200..<300).contains(http.statusCode) else {
                throw NSError(domain: "UpdateChecker", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: "GitHub 응답 \(http.statusCode)"])
            }
            let parsed = try Self.parseRelease(data: data)
            lastCheckedAt = Date()
            lastError = nil
            latestRelease = parsed
            if Self.isVersion(parsed.version, newerThan: currentVersion) {
                announceIfNeeded(parsed)
            }
        } catch {
            lastError = error.localizedDescription
            lastCheckedAt = Date()
        }
    }

    func openReleasePage() {
        if let url = latestRelease?.htmlURL {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.open(releasesPageURL)
        }
    }

    private func announceIfNeeded(_ release: ReleaseInfo) {
        let defaults = UserDefaults.standard
        let lastSeen = defaults.string(forKey: Self.lastSeenVersionKey) ?? ""
        guard release.version != lastSeen else { return }
        defaults.set(release.version, forKey: Self.lastSeenVersionKey)
        let content = UNMutableNotificationContent()
        content.title = "Photome \(release.version) 사용 가능"
        content.body = "현재 \(currentVersion) → 새 버전 \(release.version)이 릴리스됐습니다. ‘업데이트 확인’ 메뉴에서 다운로드하세요."
        content.sound = .default
        let request = UNNotificationRequest(identifier: "photome.update.\(release.version)",
                                            content: content,
                                            trigger: nil)
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    private static func parseRelease(data: Data) throws -> ReleaseInfo {
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw URLError(.cannotParseResponse)
        }
        let tag = (json["tag_name"] as? String) ?? ""
        let urlString = (json["html_url"] as? String) ?? "https://github.com"
        let published = (json["published_at"] as? String) ?? ""
        let version = normalizedVersion(from: tag)
        guard let url = URL(string: urlString) else {
            throw URLError(.badURL)
        }
        return ReleaseInfo(version: version, tag: tag, htmlURL: url, publishedAt: published)
    }

    private static func normalizedVersion(from tag: String) -> String {
        var trimmed = tag.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.lowercased().hasPrefix("mac-v") {
            trimmed = String(trimmed.dropFirst(5))
        } else if trimmed.lowercased().hasPrefix("v") {
            trimmed = String(trimmed.dropFirst())
        }
        return trimmed
    }

    static func isVersion(_ candidate: String, newerThan baseline: String) -> Bool {
        let parsed = parseSemver(candidate)
        let base = parseSemver(baseline)
        for index in 0..<max(parsed.count, base.count) {
            let a = index < parsed.count ? parsed[index] : 0
            let b = index < base.count ? base[index] : 0
            if a != b { return a > b }
        }
        return false
    }

    private static func parseSemver(_ value: String) -> [Int] {
        let cleaned = value.split(whereSeparator: { !$0.isNumber && $0 != "." })
            .joined()
        return cleaned.split(separator: ".").compactMap { Int($0) }
    }
}

