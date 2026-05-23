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
