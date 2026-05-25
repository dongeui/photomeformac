import Foundation
import Testing
@testable import PhotomeForMac

@MainActor
@Test func webViewReloadsWhenURLChanges() {
    let oldURL = URL(string: "http://127.0.0.1:8000/error")!
    let newURL = URL(string: "http://127.0.0.1:8000/dashboard")!

    #expect(WebDashboardView.needsReload(
        currentURL: oldURL,
        currentToken: "starting",
        desiredURL: newURL,
        desiredToken: "starting"
    ))
}

@MainActor
@Test func webViewReloadsWhenBackendStateTokenChanges() {
    let dashboardURL = URL(string: "http://127.0.0.1:8000/dashboard")!

    #expect(WebDashboardView.needsReload(
        currentURL: dashboardURL,
        currentToken: "starting",
        desiredURL: dashboardURL,
        desiredToken: "running"
    ))
}

@MainActor
@Test func webViewDoesNotReloadWhenURLAndTokenMatch() {
    let dashboardURL = URL(string: "http://127.0.0.1:8000/dashboard")!

    #expect(!WebDashboardView.needsReload(
        currentURL: dashboardURL,
        currentToken: "running",
        desiredURL: dashboardURL,
        desiredToken: "running"
    ))
}

@MainActor
@Test func scanJobSummaryUsesProgressCounts() {
    let summary = BackendSupervisor.summarizeLibraryJob([
        "job_kind": "scan",
        "result": [
            "progress": [
                "scan": ["current": 12, "total": 34, "failed": 1],
                "files_found": 40
            ]
        ]
    ])

    #expect(summary == "스캔 중 · 12 / 34 · 발견 40 · 실패 1")
}

@MainActor
@Test func semanticJobSummaryUsesAggregateCounts() {
    let summary = BackendSupervisor.summarizeLibraryJob([
        "job_kind": "semantic_maintenance",
        "result": [
            "progress": [
                "chunk": 2,
                "current": 100,
                "pending": 500,
                "total_succeeded": 800,
                "total_failed": 3,
                "total_embeddings_created": 700,
                "total_auto_tag_values": 2100
            ]
        ]
    ])

    #expect(summary == "검색 분석 중 · 묶음 2 · 100 / 500 · 완료 800 · 실패 3 · AI +700 · 태그 +2100")
}
