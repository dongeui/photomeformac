from __future__ import annotations

import sys
import types

import pytest

from app.core.settings import load_settings
from app.core import telemetry


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str):
    for key in ("TROVE_CRASH_REPORTING", "TROVE_SENTRY_DSN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings()


def test_no_init_without_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    # DSN만 있고 동의가 없으면 초기화하지 않는다.
    settings = _settings(monkeypatch, TROVE_SENTRY_DSN="https://k@example.test/1")
    assert telemetry.init_crash_reporting(settings) is False


def test_no_init_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    # 동의만 있고 DSN이 없으면 초기화하지 않는다.
    settings = _settings(monkeypatch, TROVE_CRASH_REPORTING="1")
    assert telemetry.init_crash_reporting(settings) is False


def test_init_with_consent_and_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    # 동의 + DSN이 모두 있으면 sentry_sdk.init을 PII 미수집·트레이싱 0으로 호출한다.
    captured: dict = {}
    fake = types.ModuleType("sentry_sdk")
    fake.init = lambda **kwargs: captured.update(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    settings = _settings(
        monkeypatch,
        TROVE_CRASH_REPORTING="1",
        TROVE_SENTRY_DSN="https://k@example.test/1",
    )
    assert telemetry.init_crash_reporting(settings) is True
    assert captured["send_default_pii"] is False
    assert captured["traces_sample_rate"] == 0.0
    assert callable(captured["before_send"])


def test_before_send_scrubs_user_paths() -> None:
    event = {
        "server_name": "someones-macbook.local",
        "message": "failed reading /Users/alice/Photos/secret.heic",
        "exception": {"values": [{"value": "no such file: /Volumes/NAS/x.jpg"}]},
        "breadcrumbs": [{"message": "/Users/alice/q"}],
    }
    out = telemetry._before_send(event, {})

    assert "server_name" not in out
    assert "breadcrumbs" not in out
    assert "/Users/alice" not in out["message"]
    assert "/Volumes/NAS" not in out["exception"]["values"][0]["value"]
