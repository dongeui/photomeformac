"""Logging bootstrap for the application."""

from __future__ import annotations

import logging


_CONFIGURED = False


class _HealthzAccessFilter(logging.Filter):
    """갤러리 하트비트가 탭마다 5초 간격으로 /healthz를 치므로, 액세스 로그에
    그대로 남기면 회전(10MB×3)되는 백엔드 로그에서 실제 진단 기록을 밀어낸다."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("uvicorn.access").addFilter(_HealthzAccessFilter())
    _CONFIGURED = True

