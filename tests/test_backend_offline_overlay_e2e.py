"""갤러리 '연결 끊김' 오버레이 — 실브라우저 엔진(Chromium/WebKit) e2e.

브라우저 탭은 앱이 닫아줄 수 없으므로(로컬서버+브라우저 UI 앱 공통),
백엔드가 죽으면 하트비트가 오버레이를 띄우고 백엔드가 돌아오면 자동
새로고침으로 복귀해야 한다. Chrome/Edge는 둘 다 Chromium(Blink) 엔진이라
chromium 한 번이 둘을 대표하고, Safari는 webkit으로 검증한다.

playwright 미설치 환경에서는 자동 skip된다.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from tests.e2e_utils import free_port, start_backend

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_offline_overlay_appears_and_recovers(tmp_path: Path, engine: str) -> None:
    port = free_port()
    backend = start_backend(tmp_path, port)
    revived: subprocess.Popen | None = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = getattr(playwright, engine).launch()
            except Exception as exc:  # 엔진 바이너리 미설치 환경
                pytest.skip(f"{engine} launch unavailable: {exc}")
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/gallery")
            overlay = page.locator("#backend-offline")
            expect(overlay).to_be_hidden()

            # 백엔드 사망 → 하트비트(5초 주기, 3회 연속 실패 ≈ 15초)가 오버레이를 띄운다
            backend.send_signal(signal.SIGKILL)
            backend.wait(timeout=10)
            expect(overlay).to_be_visible(timeout=30_000)
            expect(overlay).to_contain_text("Trove 연결이 끊겼습니다")

            # 백엔드 부활 → 자동 새로고침으로 복귀, 오버레이는 다시 숨김
            revived = start_backend(tmp_path, port)
            expect(overlay).to_be_hidden(timeout=25_000)
            expect(page.locator("#gallery-search-form")).to_be_visible(timeout=10_000)

            browser.close()
    finally:
        for proc in (backend, revived):
            if proc is not None and proc.poll() is None:
                proc.kill()
