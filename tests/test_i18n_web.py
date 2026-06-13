"""웹 i18n 회귀 가드 — 갤러리 한/영 렌더 + 언어 전환 쿠키 + 오픈리다이렉트 방어."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.settings import load_settings
from app.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(tmp_path / "photos"))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(tmp_path / "data" / "t.sqlite3"))
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")
    monkeypatch.delenv("TROVE_LOCALE", raising=False)
    (tmp_path / "photos").mkdir(parents=True, exist_ok=True)
    app = create_app(load_settings())
    with TestClient(app) as test_client:
        yield test_client


def test_gallery_defaults_to_korean(client: TestClient) -> None:
    html = client.get("/gallery").text
    assert "모든 사진" in html
    assert '<html lang="ko">' in html


def test_gallery_honors_accept_language_english(client: TestClient) -> None:
    html = client.get("/gallery", headers={"Accept-Language": "en-US,en;q=0.9"}).text
    assert "All Photos" in html
    assert "모든 사진" not in html
    assert '<html lang="en">' in html


def test_language_switch_sets_cookie_and_sticks(client: TestClient) -> None:
    resp = client.get("/lang/en?next=/gallery", follow_redirects=False)
    assert resp.status_code == 303
    assert "trove_locale=en" in resp.headers.get("set-cookie", "")
    # 쿠키가 적용된 후속 요청은 영어 (Accept-Language 무관)
    html = client.get("/gallery", headers={"Accept-Language": "ko-KR"}).text
    assert "All Photos" in html


def test_language_switch_rejects_open_redirect(client: TestClient) -> None:
    resp = client.get("/lang/ko?next=//evil.example.com", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/gallery"


def test_unsupported_language_code_does_not_set_cookie(client: TestClient) -> None:
    resp = client.get("/lang/fr?next=/gallery", follow_redirects=False)
    assert "trove_locale" not in resp.headers.get("set-cookie", "")
