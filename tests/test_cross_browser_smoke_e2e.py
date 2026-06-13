"""크로스브라우저 최소 검증(smoke) — 실브라우저 엔진(Chromium/WebKit) e2e.

Photome UI는 사용자의 기본 브라우저에서 열리므로 최소한 Chrome/Edge/Safari
에서 깨지지 않아야 한다. Chrome/Edge는 둘 다 Chromium(Blink) 엔진이라
chromium 한 번이 둘을 대표하고, Safari는 webkit으로 검증한다.

검증 범위(최소):
  - /gallery 가 200으로 로드되고 검색 폼이 보인다
  - 페이지 로드~검색 제출까지 JS 미처리 예외(pageerror)가 없다
  - 검색 제출이 200으로 응답한다 (빈 라이브러리여도 페이지는 멀쩡해야 함)

playwright 미설치 환경에서는 자동 skip된다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e_utils import free_port, start_backend

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_gallery_smoke(tmp_path: Path, engine: str) -> None:
    port = free_port()
    backend = start_backend(tmp_path, port)
    try:
        with sync_playwright() as playwright:
            try:
                browser = getattr(playwright, engine).launch()
            except Exception as exc:  # 엔진 바이너리 미설치 환경
                pytest.skip(f"{engine} launch unavailable: {exc}")
            page = browser.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            response = page.goto(f"http://127.0.0.1:{port}/gallery")
            assert response is not None and response.status == 200
            expect(page.locator("#gallery-search-form")).to_be_visible()
            expect(page.locator("input[name='q']")).to_be_visible()

            page.fill("input[name='q']", "테스트")
            with page.expect_navigation() as nav:
                page.press("input[name='q']", "Enter")
            assert nav.value.status == 200
            expect(page.locator("#gallery-search-form")).to_be_visible()

            assert not page_errors, f"{engine} JS 미처리 예외: {page_errors}"
            browser.close()
    finally:
        if backend.poll() is None:
            backend.kill()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_gallery_filter_popover(tmp_path: Path, engine: str) -> None:
    """상단 검색바의 '필터' 팝오버: 기본 숨김 → 클릭 시 기간·인물 노출 →
    바깥 클릭 시 닫힘."""
    port = free_port()
    backend = start_backend(tmp_path, port)
    try:
        with sync_playwright() as playwright:
            try:
                browser = getattr(playwright, engine).launch()
            except Exception as exc:  # 엔진 바이너리 미설치 환경
                pytest.skip(f"{engine} launch unavailable: {exc}")
            page = browser.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            page.goto(f"http://127.0.0.1:{port}/gallery")
            pop = page.locator("#filter-pop")
            expect(page.locator("#filter-toggle")).to_be_visible()
            expect(pop).to_be_hidden()

            page.click("#filter-toggle")
            expect(pop).to_be_visible()
            expect(pop.locator("input[name='date_from']")).to_be_visible()
            expect(pop.locator("input[name='date_to']")).to_be_visible()
            expect(pop.locator("select[name='person']")).to_have_count(1)

            # 바깥(갤러리 영역) 클릭 시 닫힌다
            page.click("section.content", position={"x": 5, "y": 5})
            expect(pop).to_be_hidden()

            assert not page_errors, f"{engine} JS 미처리 예외: {page_errors}"
            browser.close()
    finally:
        if backend.poll() is None:
            backend.kill()
