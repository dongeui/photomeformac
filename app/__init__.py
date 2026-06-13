"""Trove application package."""

# 어떤 서브모듈이 import 시점에 TROVE_* 환경변수를 직접 읽더라도 기존 설치의
# 레거시(PHOTOME_/PHOTOMINE_) 값이 잡히도록, 패키지 로드 시 가장 먼저 승격한다.
from app.core.envcompat import normalize_environment as _normalize_environment

_normalize_environment()
