"""경량 i18n — 키 기반 문자열 카탈로그.

설계 목표:
- 한국어/영어를 기본 지원하되, 로케일 추가는 ``app/locales/<code>.json`` 파일
  하나만 더 두면 되도록 한다(코드 변경 없음).
- 누락 키는 ko로 폴백하고, ko에도 없으면 키 자체를 반환해(개발 중 눈에 띄게)
  화면이 비지 않게 한다.
- 서버 렌더링 HTML에서 매 요청 로케일로 ``t(key, locale, **fmt)``를 호출한다.

로케일 해석 우선순위(resolve_locale): 쿠키(trove_locale) → 설정 기본값
(TROVE_LOCALE, mac 앱 첫 실행 선택값) → Accept-Language → DEFAULT_LOCALE.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DEFAULT_LOCALE = "ko"
# UI에 노출할 지원 로케일. 라벨은 해당 언어로 적어 사용자가 자기 언어를 찾기 쉽게.
SUPPORTED_LOCALES: dict[str, str] = {
    "ko": "한국어",
    "en": "English",
}

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"


@lru_cache(maxsize=None)
def _catalog(locale: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{locale}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def normalize_locale(value: str | None) -> str:
    """'en-US', 'EN' 같은 입력을 지원 로케일 코드로 정규화. 미지원이면 기본값."""
    if not value:
        return DEFAULT_LOCALE
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LOCALES else DEFAULT_LOCALE


def t(key: str, locale: str = DEFAULT_LOCALE, /, **fmt: object) -> str:
    """키를 로케일 문자열로 변환. fmt가 있으면 str.format으로 치환한다.

    폴백: locale 카탈로그 → ko 카탈로그 → 키 자체.
    """
    catalog = _catalog(locale)
    template = catalog.get(key)
    if template is None and locale != DEFAULT_LOCALE:
        template = _catalog(DEFAULT_LOCALE).get(key)
    if template is None:
        template = key
    if not fmt:
        return template
    try:
        return template.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return template


def subset(locale: str, prefix: str) -> dict[str, str]:
    """주어진 접두사로 시작하는 키들을 {접두사 제거한 키: 번역값}으로 반환.

    JS에 번역 묶음을 한 번에 주입할 때 쓴다(예: prefix='djs.' → const DT).
    locale에 없는 키는 ko로 폴백. 반환 키는 JS 식별자로 쓰도록 '.' 없는
    이름을 권장(djs.tag_object → DT.tag_object).
    """
    resolved = normalize_locale(locale)
    base = _catalog(DEFAULT_LOCALE)
    loc = _catalog(resolved)
    out: dict[str, str] = {}
    for key in base:
        if key.startswith(prefix):
            short = key[len(prefix):]
            out[short] = loc.get(key, base[key])
    return out


def translator(locale: str):
    """요청 단위로 locale을 고정한 t의 부분적용 버전."""
    resolved = normalize_locale(locale)

    def _t(key: str, /, **fmt: object) -> str:
        return t(key, resolved, **fmt)

    return _t


def resolve_locale(
    *,
    cookie: str | None = None,
    configured_default: str | None = None,
    accept_language: str | None = None,
) -> str:
    """요청 컨텍스트에서 사용할 로케일을 결정한다(우선순위는 모듈 docstring 참고)."""
    if cookie:
        code = normalize_locale(cookie)
        if code in SUPPORTED_LOCALES and cookie.strip().lower().split("-", 1)[0] in SUPPORTED_LOCALES:
            return code
    if configured_default:
        code = normalize_locale(configured_default)
        if code in SUPPORTED_LOCALES:
            return code
    if accept_language:
        # "ko-KR,ko;q=0.9,en;q=0.8" 같은 헤더에서 첫 지원 로케일을 고른다.
        for part in accept_language.split(","):
            token = part.split(";", 1)[0].strip()
            code = token.lower().split("-", 1)[0]
            if code in SUPPORTED_LOCALES:
                return code
    return DEFAULT_LOCALE
