"""Opt-in 크래시/예외 리포팅 (Sentry).

설계 원칙:
- **opt-in 전용.** 사용자가 명시적으로 동의(`TROVE_CRASH_REPORTING=1`)하고 DSN이
  주입된 경우에만 초기화한다. 둘 중 하나라도 없으면 완전한 no-op이다.
- **선택 의존성.** `sentry-sdk`는 optional 패키지다. 미설치 환경(base import
  path)에서도 import/startup이 깨지면 안 되므로 import를 가드한다.
- **콘텐츠 미수집.** 사진·파일 경로·검색어 같은 사용자 콘텐츠는 보내지 않는다.
  크래시/예외 스택만 보낸다. `send_default_pii=False` + `before_send`에서 경로성
  필드를 한 번 더 제거한다. 성능 트레이싱(transactions)도 끈다.
"""

from __future__ import annotations

import logging
import re

from app.core.settings import AppSettings

logger = logging.getLogger(__name__)

# 절대경로처럼 보이는 토큰을 마스킹한다 (예외 메시지에 사용자 폴더 경로가 섞여
# 들어오는 경우 방어). /Users/<name>/... , /Volumes/... 등.
_PATH_RE = re.compile(r"(/(?:Users|Volumes|home|private|var|tmp)/[^\s\"']*)")


def _scrub_text(text: str) -> str:
    return _PATH_RE.sub("<path>", text)


def _before_send(event: dict, hint: dict) -> dict:  # type: ignore[type-arg]
    # 서버 이름(호스트명)·사용자 경로가 묻어 나갈 수 있는 필드를 제거/마스킹한다.
    event.pop("server_name", None)
    event.pop("modules", None)
    # 예외 메시지의 경로 마스킹.
    for exc in (event.get("exception") or {}).get("values") or []:
        if isinstance(exc.get("value"), str):
            exc["value"] = _scrub_text(exc["value"])
    if isinstance(event.get("message"), str):
        event["message"] = _scrub_text(event["message"])
    # breadcrumbs에는 파일 경로/쿼리가 섞이기 쉬우므로 통째로 비운다.
    event.pop("breadcrumbs", None)
    return event


def init_crash_reporting(settings: AppSettings) -> bool:
    """동의 + DSN이 모두 있을 때만 Sentry를 초기화한다. 초기화 여부를 반환한다."""
    if not settings.crash_reporting_enabled or not settings.sentry_dsn.strip():
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.info("crash reporting requested but sentry-sdk is not installed; skipping")
        return False

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.strip(),
            release=f"{settings.app_name}@{settings.app_version}",
            # 크래시/예외만. 성능 트레이싱·프로파일링은 끈다.
            traces_sample_rate=0.0,
            # 사용자 콘텐츠·IP 등 PII 자동 수집 금지.
            send_default_pii=False,
            max_breadcrumbs=0,
            before_send=_before_send,
        )
    except Exception:  # noqa: BLE001 — 텔레메트리 초기화 실패가 앱을 죽이면 안 된다.
        logger.warning("failed to initialize crash reporting", exc_info=True)
        return False

    logger.info("crash reporting initialized (opt-in)")
    return True
