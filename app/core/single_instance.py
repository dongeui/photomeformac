"""Single-instance enforcement and supervisor watchdog for the local backend.

맥앱은 시작할 때마다 백엔드 프로세스를 새로 띄운다. 이전 백엔드가 살아남으면
같은 SQLite data_root를 여러 프로세스가 동시에 쓰면서 잠금 경합과 중복
스케줄러 실행이 생긴다(시맨틱 유지보수 청크 전체 실패의 원인).

정책은 "새 인스턴스 승리":
- 시작 시 lockfile에 기록된 이전 인스턴스와, 같은 data_root를 쓰는 떠돌이
  백엔드 프로세스를 모두 종료한 뒤 자기 자신을 lockfile에 기록한다.
- 맥앱이 비정상 종료해 백엔드가 고아가 되는 경우는 parent watchdog이
  부모 사망(재부모화)을 감지해 스스로 종료한다(TROVE_SUPERVISED=1일 때).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = "backend-instance.json"

# 백엔드 프로세스 식별용 커맨드라인 마커. 맥앱(`python -c "from app.main import
# main; main()"`)과 모듈 실행(`python -m app.main`) 둘 다 잡는다.
_BACKEND_COMMAND_MARKERS = ("from app.main import main", "app.main")


def instance_lock_path(data_root: Path) -> Path:
    return Path(data_root) / LOCK_FILENAME


def enforce_single_instance(data_root: Path, port: int) -> None:
    """Terminate other backend instances sharing this data_root, then record ours.

    Best-effort: 어떤 단계가 실패해도 서버 기동 자체는 막지 않는다.
    """
    try:
        data_root = Path(data_root)
        data_root.mkdir(parents=True, exist_ok=True)
        lock_path = instance_lock_path(data_root)
        _evict_lockfile_instance(lock_path)
        _evict_same_data_root_instances(data_root)
        _write_instance_lock(lock_path, port)
    except Exception as exc:
        logger.warning("single-instance enforcement failed", extra={"error": str(exc)})


def start_parent_watchdog() -> None:
    """Shut the backend down when its supervisor (the mac app) dies.

    macOS에는 parent-death signal이 없어서, 맥앱이 강제 종료/크래시하면 자식
    백엔드가 launchd(pid 1)로 재부모화된 채 살아남는다. TROVE_SUPERVISED=1로
    띄워진 경우 ppid 변화를 감시해 스스로 graceful 종료한다.
    """
    if os.environ.get("TROVE_SUPERVISED") != "1":
        return
    initial_ppid = os.getppid()
    if initial_ppid <= 1:
        # 이미 고아 상태로 시작됨 — 감시 무의미.
        return

    def _watch() -> None:
        while True:
            time.sleep(5)
            if os.getppid() != initial_ppid:
                logger.warning("supervisor exited — shutting down orphaned backend")
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except OSError:
                    pass
                time.sleep(15)
                os._exit(0)

    threading.Thread(target=_watch, daemon=True, name="parent-watchdog").start()


def _evict_lockfile_instance(lock_path: Path) -> None:
    payload = _read_lock(lock_path)
    if payload is None:
        return
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid == os.getpid() or not _pid_alive(pid):
        return
    if not _is_backend_process(pid):
        # PID 재사용으로 다른 프로세스가 잡힌 경우 — 건드리지 않는다.
        return
    logger.info("terminating previous backend instance", extra={"pid": pid})
    _terminate_pid(pid)


def _evict_same_data_root_instances(data_root: Path) -> None:
    """lockfile에 없더라도 같은 data_root를 쓰는 떠돌이 백엔드를 정리한다.

    환경변수에 data_root 경로가 들어있는 프로세스만 종료하므로, 병렬로 운영하는
    다른 Trove 설치본(다른 data_root)은 건드리지 않는다.
    """
    needle = str(data_root)
    for pid in _list_backend_pids():
        if pid == os.getpid():
            continue
        environ = _process_environ_text(pid)
        if environ and needle in environ:
            logger.info("terminating stray backend with same data_root", extra={"pid": pid})
            _terminate_pid(pid)


def _write_instance_lock(lock_path: Path, port: int) -> None:
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": int(port),
                "started_at": datetime.utcnow().isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _read_lock(lock_path: Path) -> dict | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def _process_environ_text(pid: int) -> str:
    """같은 사용자 소유 프로세스의 환경변수 문자열(macOS `ps -E`)."""
    try:
        result = subprocess.run(
            ["ps", "-E", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def _is_backend_process(pid: int) -> bool:
    command = _process_command(pid)
    return bool(command) and any(marker in command for marker in _BACKEND_COMMAND_MARKERS)


def _list_backend_pids() -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", _BACKEND_COMMAND_MARKERS[0]],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in result.stdout.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _terminate_pid(pid: int, timeout_seconds: float = 8.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(0.2)
    return not _pid_alive(pid)
