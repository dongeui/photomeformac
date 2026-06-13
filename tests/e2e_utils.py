"""실브라우저(e2e) 테스트 공용 헬퍼 — 격리된 백엔드 프로세스 기동."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_backend(tmp_path: Path, port: int) -> subprocess.Popen:
    data_root = tmp_path / "data"
    source_root = tmp_path / "photos"
    source_root.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "TROVE_SERVER_HOST": "127.0.0.1",
        "TROVE_SERVER_PORT": str(port),
        "TROVE_SOURCE_ROOTS": str(source_root),
        "TROVE_DATA_ROOT": str(data_root),
        "TROVE_DERIVED_ROOT": str(tmp_path / "derived"),
        "TROVE_DATABASE_PATH": str(data_root / "photome.sqlite3"),
        "TROVE_OFFLINE_MODE": "1",
        "TROVE_CLIP_ENABLED": "0",
        "TROVE_FACE_ANALYSIS_ENABLED": "0",
        "TROVE_SYNC_SCHEDULER_ENABLED": "0",
        "TROVE_LOG_LEVEL": "ERROR",
        # 헤드리스 브라우저는 Accept-Language: en을 보내므로, 한국어 문구를
        # 검증하는 e2e가 흔들리지 않게 로케일을 ko로 고정한다.
        "TROVE_LOCALE": "ko",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "from app.main import main; main()"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return process
        except Exception:
            if process.poll() is not None:
                raise RuntimeError("backend exited during startup")
            time.sleep(0.3)
    process.kill()
    raise RuntimeError("backend did not become healthy in 30s")
