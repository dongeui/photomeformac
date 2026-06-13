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


def test_mac_app_env_lan_mode_must_be_explicit(tmp_path: Path) -> None:
    local_env = build_backend_env(tmp_path)
    lan_env = build_backend_env(tmp_path, lan=True)

    assert local_env["TROVE_SERVER_HOST"] == "127.0.0.1"
    assert "TROVE_LAN_ADMIN_TOKEN" not in local_env
    assert lan_env["TROVE_SERVER_HOST"] == "0.0.0.0"
    assert lan_env["TROVE_LAN_ADMIN_TOKEN"]
    assert (tmp_path / "lan-admin-token").exists()


def test_mac_app_env_can_disable_clip_without_disabling_app(tmp_path: Path) -> None:
    env = build_backend_env(tmp_path, clip_enabled=False)

    assert env["TROVE_CLIP_ENABLED"] == "0"
    assert env["TROVE_OFFLINE_MODE"] == "1"
