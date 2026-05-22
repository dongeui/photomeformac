#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${PHOTOME_REPO_ROOT:-/Users/dongeui/Desktop/code/photomeformac}"
cd "$REPO_ROOT"

if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.11)"
else
  PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e '.[test]'

cat <<MSG
완료:
PHOTOME_REPO_ROOT=$REPO_ROOT
PHOTOME_PYTHON=$REPO_ROOT/.venv/bin/python
MSG
