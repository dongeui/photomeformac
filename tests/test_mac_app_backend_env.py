from __future__ import annotations

from pathlib import Path

from scripts.mac_app_backend_env import build_backend_env


def test_mac_app_env_uses_app_data_paths(tmp_path: Path) -> None:
    env = build_backend_env(tmp_path)

    assert env["PHOTOME_SERVER_HOST"] == "127.0.0.1"
    assert env["PHOTOME_SERVER_PORT"] == "8000"
    assert env["PHOTOME_DATA_ROOT"] == str(tmp_path / "data")
    assert env["PHOTOME_DERIVED_ROOT"] == str(tmp_path / "derived")
    assert env["PHOTOME_MODEL_ROOT"] == str(tmp_path / "models")
    assert env["PHOTOME_GEODATA_ROOT"] == str(tmp_path / "models" / "geodata")
    assert env["PHOTOME_DATABASE_PATH"] == str(tmp_path / "data" / "photome.sqlite3")
    assert env["PHOTOME_OFFLINE_MODE"] == "1"


def test_mac_app_env_does_not_rewrite_source_roots_to_docker_paths(tmp_path: Path) -> None:
    photos = tmp_path / "Pictures"
    nas = Path("/Volumes/NAS/Photos")

    env = build_backend_env(tmp_path / "AppData", source_roots=[photos, nas])

    assert env["PHOTOME_SOURCE_ROOTS"] == f"{photos},{nas}"
    assert "/photos" not in env["PHOTOME_SOURCE_ROOTS"]
    assert "PHOTOME_SOURCE_ROOT_MOUNT" not in env
    assert "PHOTOME_SOURCE_ROOT_HOST" not in env


def test_mac_app_env_lan_mode_must_be_explicit(tmp_path: Path) -> None:
    local_env = build_backend_env(tmp_path)
    lan_env = build_backend_env(tmp_path, lan=True)

    assert local_env["PHOTOME_SERVER_HOST"] == "127.0.0.1"
    assert "PHOTOME_LAN_ADMIN_TOKEN" not in local_env
    assert lan_env["PHOTOME_SERVER_HOST"] == "0.0.0.0"
    assert lan_env["PHOTOME_LAN_ADMIN_TOKEN"]
    assert (tmp_path / "lan-admin-token").exists()


def test_mac_app_env_can_disable_clip_without_disabling_app(tmp_path: Path) -> None:
    env = build_backend_env(tmp_path, clip_enabled=False)

    assert env["PHOTOME_CLIP_ENABLED"] == "0"
    assert env["PHOTOME_OFFLINE_MODE"] == "1"
