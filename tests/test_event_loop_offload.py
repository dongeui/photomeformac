"""이벤트 루프 보호 회귀 가드.

무거운 동기 작업(DB 조회, 하이브리드 검색)을 하는 핸들러는 sync def여야
FastAPI가 threadpool에서 돌린다. async def로 되돌아가면 검색 한 건이
서버 전체(/healthz 포함)를 멈춰 '연결 끊김' 오버레이가 오탐된다.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from app.api import gallery, media, search
from app.core.logging import _HealthzAccessFilter


@pytest.mark.parametrize(
    "handler",
    [
        gallery.home_page,
        gallery.gallery_page,
        gallery.media_download,
        gallery.gallery_asset,
        search.search_media,
        search.search_media_debug,
        search.search_suggest,
        search.search_benchmark,
        media.filter_media,
        media.list_media,
        media.get_media,
    ],
    ids=lambda fn: f"{fn.__module__.rsplit('.', 1)[-1]}.{fn.__name__}",
)
def test_heavy_handlers_run_in_threadpool(handler) -> None:
    assert not inspect.iscoroutinefunction(handler), (
        f"{handler.__name__}는 sync def여야 한다 — async def로 바꾸면 동기 작업이 "
        "이벤트 루프를 막아 /healthz가 지연되고 오프라인 오버레이가 오탐된다"
    )


def _access_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_healthz_access_log_filtered() -> None:
    log_filter = _HealthzAccessFilter()
    assert not log_filter.filter(_access_record('127.0.0.1 - "GET /healthz HTTP/1.1" 200'))
    assert log_filter.filter(_access_record('127.0.0.1 - "GET /gallery HTTP/1.1" 200'))
