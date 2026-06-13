#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${TROVE_REPO_ROOT:-/Users/dongeui/Desktop/code/photomeformac}"
cd "$REPO_ROOT"

if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.11)"
else
  PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -U pip
# 배포 산출물은 ai-pack 단일 빌드 — clip(torch/open_clip)은 선택이 아니라 필수다.
# 이걸 빼고 부트스트랩하면 백엔드가 base로 떠서 이미지 AI가 조용히 멈춘다.
.venv/bin/python -m pip install -e '.[test,clip]'

cat <<MSG
완료:
TROVE_REPO_ROOT=$REPO_ROOT
TROVE_PYTHON=$REPO_ROOT/.venv/bin/python
MSG
