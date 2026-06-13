from __future__ import annotations

from pathlib import Path


def test_backend_supervisor_wires_real_log_file_support() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/BackendSupervisor.swift"
    ).read_text(encoding="utf-8")

    assert "@Published private(set) var logFileURL: URL?" in content
    assert "prepareLogFile(appDataRoot:" in content
    assert "trove-backend.log" in content
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
    assert "self.sourceRoots = UserDefaults.standard.stringArray" in content
    assert "self.lanEnabled = UserDefaults.standard.bool" in content
    # CLIP과 offlineMode는 정식 배포에서 항상 켜진/차단된 상수. 토글/UserDefaults 없음.
    assert "let clipEnabled: Bool = true" in content
    assert "let offlineMode: Bool = true" in content
    assert "UserDefaults.standard.set(lanEnabled, forKey: Self.lanEnabledDefaultsKey)" in content
    assert "self.sourceRoots = paths" in content
    # 번들 weights → user data 1회 복사 helper가 존재해야 함
    assert "seedPreinstalledModels" in content


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
    # 통합 동기화: 수동 진입점은 triggerLibraryScan 하나다. 별도 이미지 AI
    # 트리거와 scheduler.background_task 폴백은 제거됐다.
    assert 'triggerSemanticMaintenance' not in content
    assert 'background_task_kind' not in content
    assert 'static func parseLibraryJobStatus(payload: [String: Any]?) -> LibraryJobStatus?' in content
    assert 'static func summarizeLibraryJob(_ job: [String: Any]) -> String {' in content


def test_menu_bar_only_app_structure() -> None:
    content = Path(
        "mac/PhotomeForMac/Sources/PhotomeForMac/PhotomeForMacApp.swift"
    ).read_text(encoding="utf-8")

    # 창 없는 메뉴바 전용 앱: MenuBarExtra만, accessory 정책으로 Dock/상단 메뉴 숨김
    assert "MenuBarExtra(" in content
    assert "setActivationPolicy(.accessory)" in content
    assert "Window(" not in content
    # 사진첩 열기(기본 브라우저) + 폴더 선택을 한 단계 평면으로 (중첩 Menu 미사용)
    assert 'Button(Localized.s("사진첩 열기"))' in content
    assert "backend.openGallery()" in content
    assert 'Menu("고급")' not in content
    assert "backend.choosePhotoFolder()" in content
    # 동기화/이미지 AI 제어는 웹 "설정" 탭으로 이전 — 메뉴바에서 제거
    assert 'Button("전체 동기화 시작")' not in content
    assert 'Button("이미지 AI 이어서 분석")' not in content
    # 로그/진단/모델 항목도 메뉴바에서 제거
    assert "backend.showLogs()" not in content
    assert "backend.prepareAIModel" not in content
