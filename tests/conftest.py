from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_real_face_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTOMINE_FACE_ANALYSIS_ENABLED", "0")
