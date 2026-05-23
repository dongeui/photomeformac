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
