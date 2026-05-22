from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.mac_app_smoke import build_backend_command, redact_env_for_log


def test_build_backend_command_uses_current_python_module_entrypoint() -> None:
    command = build_backend_command()

    assert command[0]
    assert command[-2:] == ["-c", "from app.main import main; main()"]


def test_redact_env_for_log_keeps_photome_values_only(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PHOTOME_DATA_ROOT": str(tmp_path / "data"),
        "PHOTOME_SERVER_HOST": "127.0.0.1",
        "SECRET_TOKEN": "do-not-log",
    }

    redacted = redact_env_for_log(env)

    assert redacted == {
        "PHOTOME_DATA_ROOT": str(tmp_path / "data"),
        "PHOTOME_SERVER_HOST": "127.0.0.1",
    }


def test_script_can_run_as_file_for_help() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/mac_app_smoke.py", "--help"],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Photome Mac 앱 백엔드 스모크 테스트" in result.stdout
