"""i18n 엔진 회귀 가드 — 카탈로그 조회·폴백·로케일 해석."""

from __future__ import annotations

from app.core import i18n


def test_translates_known_key_per_locale() -> None:
    assert i18n.t("nav.all_photos", "ko") == "모든 사진"
    assert i18n.t("nav.all_photos", "en") == "All Photos"


def test_format_interpolation() -> None:
    assert i18n.t("gallery.count_photos", "ko", count=12) == "12장"
    assert i18n.t("gallery.count_photos", "en", count=12) == "12 photos"


def test_missing_key_falls_back_to_ko_then_key() -> None:
    # en 카탈로그에 없는 키는 ko로 폴백
    assert i18n.t("__nope__", "en") == "__nope__"  # 둘 다 없으면 키 자체


def test_ko_and_en_catalogs_have_identical_keys() -> None:
    ko = i18n._catalog("ko")
    en = i18n._catalog("en")
    assert ko and en
    assert set(ko) == set(en), f"카탈로그 키 불일치: {set(ko) ^ set(en)}"


def test_normalize_locale() -> None:
    assert i18n.normalize_locale("en-US") == "en"
    assert i18n.normalize_locale("KO") == "ko"
    assert i18n.normalize_locale("fr") == "ko"  # 미지원 → 기본값
    assert i18n.normalize_locale(None) == "ko"


def test_resolve_locale_priority() -> None:
    # 쿠키가 최우선
    assert i18n.resolve_locale(cookie="en", configured_default="ko", accept_language="ko") == "en"
    # 쿠키 없으면 설정 기본값
    assert i18n.resolve_locale(cookie=None, configured_default="en", accept_language="ko-KR") == "en"
    # 둘 다 없으면 Accept-Language
    assert i18n.resolve_locale(cookie=None, configured_default=None, accept_language="en-US,en;q=0.9") == "en"
    # 아무것도 없으면 기본값
    assert i18n.resolve_locale() == "ko"
