#!/usr/bin/env python3
"""Mac 앱 모드 백엔드 실행 스모크 테스트."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.mac_app_backend_env import build_backend_env


def build_backend_command() -> list[str]:
    """현재 Python으로 Trove 백엔드 main()을 실행하는 명령을 만든다."""
    return [sys.executable, "-c", "from app.main import main; main()"]


def redact_env_for_log(env: Mapping[str, str]) -> dict[str, str]:
    """로그용으로 TROVE_* 값만 남긴다."""
    return {key: value for key, value in sorted(env.items()) if key.startswith("TROVE_")}


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_url(url: str, *, timeout_seconds: float = 30.0) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"백엔드 health 확인 실패: {url}: {last_error}")


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def run_smoke(app_data_root: Path, *, source_roots: list[str], timeout_seconds: float) -> dict[str, object]:
    port = find_free_port()
    env = os.environ.copy()
    env.update(
        build_backend_env(
            app_data_root,
            source_roots=source_roots,
            port=port,
            clip_enabled=False,
        )
    )
    env["TROVE_LOG_LEVEL"] = "INFO"

    command = build_backend_command()
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        payload = wait_for_url(f"http://127.0.0.1:{port}/healthz", timeout_seconds=timeout_seconds)
        return {
            "ok": True,
            "pid": process.pid,
            "port": port,
            "health": json.loads(payload.decode("utf-8")),
            "env": redact_env_for_log(env),
        }
    finally:
        terminate_process(process)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trove Mac 앱 백엔드 스모크 테스트")
    parser.add_argument("--app-data-root", help="테스트용 앱 데이터 루트. 생략하면 임시 디렉토리 사용")
    parser.add_argument("--source-root", action="append", default=[], help="원본 사진 폴더")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.app_data_root:
        result = run_smoke(Path(args.app_data_root), source_roots=args.source_root, timeout_seconds=args.timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="trove-mac-smoke-") as tmp:
            result = run_smoke(Path(tmp), source_roots=args.source_root, timeout_seconds=args.timeout)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
