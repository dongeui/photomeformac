from __future__ import annotations

from pathlib import Path

from scripts.mac_app_backend_env import build_backend_env


def test_mac_app_env_uses_app_data_paths(tmp_path: Path) -> None:
    env = build_backend_env(tmp_path)

    assert env["TROVE_SERVER_HOST"] == "127.0.0.1"
    assert env["TROVE_SERVER_PORT"] == "8000"
    assert env["TROVE_DATA_ROOT"] == str(tmp_path / "data")
    assert env["TROVE_DERIVED_ROOT"] == str(tmp_path / "derived")
    assert env["TROVE_MODEL_ROOT"] == str(tmp_path / "models")
    assert env["TROVE_GEODATA_ROOT"] == str(tmp_path / "models" / "geodata")
    assert env["TROVE_DATABASE_PATH"] == str(tmp_path / "data" / "photome.sqlite3")
    assert env["TROVE_ENV_FILE"] == str(tmp_path / "photome.env")
    assert env["HF_HOME"] == str(tmp_path / "models" / "huggingface")
    assert env["TORCH_HOME"] == str(tmp_path / "models" / "torch")
    assert env["TROVE_OFFLINE_MODE"] == "1"
    assert env["TROVE_SUPERVISED"] == "1"


def test_mac_app_env_does_not_rewrite_source_roots_to_docker_paths(tmp_path: Path) -> None:
    photos = tmp_path / "Pictures"
    nas = Path("/Volumes/NAS/Photos")

    env = build_backend_env(tmp_path / "AppData", source_roots=[photos, nas])

    assert env["TROVE_SOURCE_ROOTS"] == f"{photos},{nas}"
    assert "/photos" not in env["TROVE_SOURCE_ROOTS"]
    assert "TROVE_SOURCE_ROOT_MOUNT" not in env
    assert "TROVE_SOURCE_ROOT_HOST" not in env


def test_mac_app_env_is_always_local_only(tmp_path: Path) -> None:
    # Mac 앱의 LAN 공유는 제거됐다 — 항상 local-only로 바인딩하고 LAN admin
    # 토큰을 만들지 않는다. 네트워크 노출은 Docker/서버 배포가 담당한다.
    env = build_backend_env(tmp_path)

    assert env["TROVE_SERVER_HOST"] == "127.0.0.1"
    assert "TROVE_LAN_ADMIN_TOKEN" not in env
    assert not (tmp_path / "lan-admin-token").exists()


def test_mac_app_env_crash_reporting_is_opt_in(tmp_path: Path) -> None:
    # 기본은 미수집. 동의 + DSN이 둘 다 있을 때만 백엔드 env에 실린다.
    default_env = build_backend_env(tmp_path)
    assert "TROVE_CRASH_REPORTING" not in default_env
    assert "TROVE_SENTRY_DSN" not in default_env

    # 동의만 있고 DSN이 없으면 실리지 않는다.
    consent_only = build_backend_env(tmp_path, crash_reporting=True)
    assert "TROVE_CRASH_REPORTING" not in consent_only

    dsn = "https://k@example.test/1"
    enabled = build_backend_env(tmp_path, crash_reporting=True, sentry_dsn=dsn)
    assert enabled["TROVE_CRASH_REPORTING"] == "1"
    assert enabled["TROVE_SENTRY_DSN"] == dsn


def test_mac_app_env_can_disable_clip_without_disabling_app(tmp_path: Path) -> None:
    env = build_backend_env(tmp_path, clip_enabled=False)

    assert env["TROVE_CLIP_ENABLED"] == "0"
    assert env["TROVE_OFFLINE_MODE"] == "1"
