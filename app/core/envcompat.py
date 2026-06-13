"""환경변수 접두사 호환 resolver.

캐노니컬 접두사는 ``TROVE_``. 브랜드 리네임 이전의 ``PHOTOME_``와 그 이전의
``PHOTOMINE_``는 레거시 별칭으로 계속 읽는다. 기존 설치의 photome.env(여전히
PHOTOME_* 키)가 마이그레이션 없이 그대로 동작하게 하기 위한 것이다.

읽기 시점에 해석하므로(모듈 import 시점이 아니라) import 순서와 무관하게
안전하다. 서비스 곳곳의 직접 ``os.environ.get`` 호출을 이 모듈로 대체한다.
"""

from __future__ import annotations

import os

_CANONICAL_PREFIX = "TROVE_"
# 우선순위 순서대로 시도: 캐노니컬 → 옛 브랜드 → 그 이전 브랜드.
_LEGACY_PREFIXES = ("PHOTOME_", "PHOTOMINE_")


def _candidates(name: str) -> list[str]:
    if name.startswith(_CANONICAL_PREFIX):
        suffix = name[len(_CANONICAL_PREFIX):]
        return [name, *(prefix + suffix for prefix in _LEGACY_PREFIXES)]
    # 혹시 레거시 이름으로 들어와도 캐노니컬을 먼저 본다.
    for prefix in _LEGACY_PREFIXES:
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            return [_CANONICAL_PREFIX + suffix, name]
    return [name]


def env_value(name: str) -> str | None:
    """캐노니컬/레거시 접두사를 순서대로 조회해 첫 비어있지 않은 값을 반환."""
    for candidate in _candidates(name):
        value = os.getenv(candidate)
        if value is not None and value != "":
            return value
    return None


def getenv(name: str, default: str | None = None) -> str | None:
    """``os.environ.get`` 대체. 레거시 접두사 폴백 포함."""
    value = env_value(name)
    return default if value is None else value


def normalize_environment() -> None:
    """레거시 접두사(PHOTOME_/PHOTOMINE_) 환경변수를 TROVE_*로 승격한다.

    ``app/__init__.py``에서 한 번 호출돼, 어떤 서브모듈이 import 시점에
    ``os.environ.get("TROVE_X")``를 직접 읽더라도 기존 설치의 PHOTOME_* 값이
    잡히게 한다. setdefault 의미라 이미 TROVE_가 있으면 건드리지 않는다.
    PHOTOME_가 PHOTOMINE_보다 우선한다(legacy 순서가 뒤일수록 먼저 적용)."""
    for legacy_prefix in reversed(_LEGACY_PREFIXES):
        for key, value in list(os.environ.items()):
            if key.startswith(legacy_prefix) and value != "":
                os.environ.setdefault(_CANONICAL_PREFIX + key[len(legacy_prefix):], value)
