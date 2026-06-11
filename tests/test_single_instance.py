from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from app.core import single_instance as si


@pytest.fixture
def quiet_process_tools(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """실제 프로세스를 건드리지 않도록 종료/조회를 모두 가짜로 바꾼다."""
    terminated: list[int] = []
    monkeypatch.setattr(si, "_terminate_pid", lambda pid, **_: terminated.append(pid) or True)
    monkeypatch.setattr(si, "_list_backend_pids", lambda: [])
    monkeypatch.setattr(si, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(si, "_is_backend_process", lambda pid: False)
    return terminated


def test_enforce_terminates_previous_lockfile_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_process_tools: list[int]
) -> None:
    monkeypatch.setattr(si, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(si, "_is_backend_process", lambda pid: pid == 4242)
    lock = si.instance_lock_path(tmp_path)
    lock.write_text(json.dumps({"pid": 4242, "port": 8000}), encoding="utf-8")

    si.enforce_single_instance(tmp_path, 8001)

    assert quiet_process_tools == [4242]
    payload = json.loads(lock.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["port"] == 8001


def test_enforce_ignores_dead_or_reused_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_process_tools: list[int]
) -> None:
    # 죽은 pid → 종료 시도 없음
    lock = si.instance_lock_path(tmp_path)
    lock.write_text(json.dumps({"pid": 5555, "port": 8000}), encoding="utf-8")
    si.enforce_single_instance(tmp_path, 8000)
    assert quiet_process_tools == []

    # 살아있지만 백엔드가 아닌 프로세스(PID 재사용) → 건드리지 않음
    monkeypatch.setattr(si, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(si, "_is_backend_process", lambda pid: False)
    lock.write_text(json.dumps({"pid": 5555, "port": 8000}), encoding="utf-8")
    si.enforce_single_instance(tmp_path, 8000)
    assert quiet_process_tools == []


def test_enforce_sweeps_only_same_data_root_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_process_tools: list[int]
) -> None:
    monkeypatch.setattr(si, "_list_backend_pids", lambda: [111, 222, os.getpid()])
    environs = {
        111: f"PHOTOME_DATA_ROOT={tmp_path} PATH=/usr/bin",
        222: "PHOTOME_DATA_ROOT=/somewhere/else PATH=/usr/bin",
    }
    monkeypatch.setattr(si, "_process_environ_text", lambda pid: environs.get(pid, ""))

    si.enforce_single_instance(tmp_path, 8000)

    assert quiet_process_tools == [111]


def test_enforce_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[int]:
        raise RuntimeError("ps unavailable")

    monkeypatch.setattr(si, "_list_backend_pids", boom)
    si.enforce_single_instance(tmp_path, 8000)  # 예외가 새어 나오면 테스트 실패


def test_parent_watchdog_requires_supervised_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTOME_SUPERVISED", raising=False)
    si.start_parent_watchdog()
    assert "parent-watchdog" not in [thread.name for thread in threading.enumerate()]
