from __future__ import annotations

from pathlib import Path


def test_webview_reloads_when_backend_state_token_changes() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/ContentView.swift"
    ).read_text(encoding="utf-8")

    assert "makeCoordinator()" in content
    assert "lastReloadToken" in content
    assert "static func needsReload" in content
    assert "currentToken != desiredToken" in content
    assert "context.coordinator.lastReloadToken = reloadToken" in content


def test_backend_supervisor_wires_real_log_file_support() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/BackendSupervisor.swift"
    ).read_text(encoding="utf-8")

    assert "@Published private(set) var logFileURL: URL?" in content
    assert "prepareLogFile(appDataRoot:" in content
    assert "photome-backend.log" in content
    assert "attachLogStreaming(pipe: pipe, logHandle: logHandle)" in content
    assert "NSWorkspace.shared.open(logFileURL)" in content
    assert "아직 생성된 로그 파일이 없습니다." in content


def test_backend_supervisor_persists_source_roots_lan_and_ai_mode_state() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/BackendSupervisor.swift"
    ).read_text(encoding="utf-8")

    assert "@Published private(set) var sourceRoots: [String]" in content
    assert 'private static let sourceRootsDefaultsKey = "PhotomeSourceRoots"' in content
    assert 'private static let lanEnabledDefaultsKey = "PhotomeLANEnabled"' in content
    assert 'private static let offlineModeDefaultsKey = "PhotomeOfflineMode"' in content
    assert "self.sourceRoots = UserDefaults.standard.stringArray" in content
    assert "self.lanEnabled = UserDefaults.standard.bool" in content
    # CLIP은 정식 배포에서 항상 켜진 상수. 토글/UserDefaults 없음.
    assert "let clipEnabled: Bool = true" in content
    assert "self.offlineMode = true" in content
    assert "UserDefaults.standard.set(lanEnabled, forKey: Self.lanEnabledDefaultsKey)" in content
    assert "UserDefaults.standard.set(offlineMode, forKey: Self.offlineModeDefaultsKey)" in content
    assert "self.sourceRoots = paths" in content
    # 번들 weights → user data 1회 복사 helper가 존재해야 함
    assert "seedPreinstalledModels" in content


def test_content_view_shows_selected_source_roots_ai_pack_and_quick_actions() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/ContentView.swift"
    ).read_text(encoding="utf-8")

    assert 'let hasFolders = !backend.sourceRoots.isEmpty' in content
    assert 'Text("선택된 폴더")' in content
    assert 'ForEach(backend.sourceRoots, id: \\.self)' in content
    assert 'backend.removeSourceRoot(path)' in content
    assert 'private var aiPackPanel: some View {' in content
    assert 'private var quickActionsPanel: some View {' in content
    assert 'Button("전체 동기화 시작")' in content
    assert 'Button("이미지 AI 이어서 분석")' in content
    assert 'backend.triggerLibraryScan()' in content
    assert 'backend.triggerSemanticMaintenance()' in content
    assert 'backend.prepareAIModel(loadCached: backend.offlineMode)' in content


def test_backend_supervisor_supports_ai_pack_status_prepare_and_library_job_status() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/BackendSupervisor.swift"
    ).read_text(encoding="utf-8")

    assert 'struct AIPackStatus: Decodable' in content
    assert 'struct LibraryJobStatus {' in content
    assert '@Published private(set) var libraryJobStatus: LibraryJobStatus?' in content
    assert 'var statusURL: URL {' in content
    assert 'func prepareAIModel(loadCached: Bool)' in content
    assert 'func triggerLibraryScan() {' in content
    assert 'func triggerSemanticMaintenance() {' in content
    assert 'static func parseLibraryJobStatus(payload: [String: Any]?) -> LibraryJobStatus?' in content
    assert 'static func summarizeLibraryJob(_ job: [String: Any]) -> String {' in content


def test_menu_bar_exposes_scan_and_semantic_actions() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/PhotomeForMacApp.swift"
    ).read_text(encoding="utf-8")

    assert 'Button("전체 동기화 시작")' in content
    assert 'Button("이미지 AI 이어서 분석")' in content
    assert 'backend.triggerLibraryScan()' in content
    assert 'backend.triggerSemanticMaintenance()' in content
    assert 'if let libraryJobStatus = backend.libraryJobStatus, backend.hasActiveLibraryJob {' in content
