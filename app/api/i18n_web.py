"""웹 요청 단위 로케일 해석 + 언어 전환 엔드포인트.

서버 렌더링 페이지(gallery/people/dashboard)는 요청마다 request_translator(request)로
``_`` 를 받아 HTML을 그 언어로 만든다. 로케일 우선순위는 i18n.resolve_locale 참고
(쿠키 trove_locale > 설정 기본값 TROVE_LOCALE > Accept-Language > ko).
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.core.i18n import SUPPORTED_LOCALES, resolve_locale, translator

LOCALE_COOKIE = "trove_locale"

router = APIRouter(tags=["i18n"])


def request_locale(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    default = getattr(settings, "default_locale", None)
    return resolve_locale(
        cookie=request.cookies.get(LOCALE_COOKIE),
        configured_default=default,
        accept_language=request.headers.get("accept-language"),
    )


def request_translator(request: Request):
    """(locale_code, translator) 반환."""
    locale = request_locale(request)
    return locale, translator(locale)


def render_lang_switcher(locale: str, request: Request) -> str:
    """사이드바 언어 전환 링크(한국어 | English). 현재 경로로 되돌아온다."""
    next_url = request.url.path
    if request.url.query:
        next_url += f"?{request.url.query}"
    links = []
    for code, label in SUPPORTED_LOCALES.items():
        active = " active" if code == locale else ""
        href = f"/lang/{code}?next={escape(next_url, quote=True)}"
        links.append(f'<a class="lang-link{active}" href="{href}">{escape(label)}</a>')
    return "".join(links)


def _safe_next(value: str | None) -> str:
    """오픈 리다이렉트 방지: 같은 출처의 절대 경로만 허용."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/gallery"


@router.get("/lang/{code}")
def switch_language(code: str, request: Request, next: str | None = None) -> RedirectResponse:
    response = RedirectResponse(_safe_next(next), status_code=303)
    # 명시적으로 지원하는 코드만 쿠키로 고정한다(미지원 코드는 무시 → 기존 해석 유지).
    requested = code.strip().lower()
    if requested in SUPPORTED_LOCALES:
        # 1년 유지. httponly 아님 — JS에서 현재 언어를 읽을 수 있어야 한다.
        response.set_cookie(LOCALE_COOKIE, requested, max_age=365 * 24 * 3600, samesite="lax")
    return response
