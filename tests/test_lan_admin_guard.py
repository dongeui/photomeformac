import os
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.core.settings import load_settings
from app.main import create_app


@contextmanager
def env(**values: str):
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_lan_admin_guard_blocks_remote_mutation_without_token(tmp_path):
    with env(
        PHOTOME_SERVER_HOST="0.0.0.0",
        PHOTOME_LAN_ADMIN_TOKEN="secret-token",
        PHOTOME_DATA_ROOT=str(tmp_path / "data"),
    ):
        app = create_app(load_settings())
        client = TestClient(app, client=("192.168.1.20", 12345))
        response = client.post("/scan")

    assert response.status_code == 401
    assert "X-Photome-Admin-Token" in response.json()["detail"]


def test_lan_admin_guard_allows_remote_mutation_with_token(tmp_path):
    with env(
        PHOTOME_SERVER_HOST="0.0.0.0",
        PHOTOME_LAN_ADMIN_TOKEN="secret-token",
        PHOTOME_DATA_ROOT=str(tmp_path / "data"),
    ):
        app = create_app(load_settings())
        client = TestClient(app, client=("192.168.1.20", 12345))
        response = client.post("/scan", headers={"X-Photome-Admin-Token": "secret-token"})

    assert response.status_code != 401


def test_lan_admin_guard_keeps_localhost_unblocked(tmp_path):
    with env(
        PHOTOME_SERVER_HOST="0.0.0.0",
        PHOTOME_LAN_ADMIN_TOKEN="secret-token",
        PHOTOME_DATA_ROOT=str(tmp_path / "data"),
    ):
        app = create_app(load_settings())
        client = TestClient(app, client=("127.0.0.1", 12345))
        response = client.post("/scan")

    assert response.status_code != 401
