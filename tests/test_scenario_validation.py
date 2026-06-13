from __future__ import annotations

import io
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.settings import load_settings
from app.main import create_app


SCAN_DELAY_SECONDS = 1.1


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_root: Path) -> Iterator[TestClient]:
    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photomine.sqlite3"

    monkeypatch.setenv("PHOTOMINE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("PHOTOMINE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("PHOTOMINE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("PHOTOMINE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("PHOTOMINE_STABILITY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("PHOTOMINE_LOG_LEVEL", "ERROR")

    app = create_app(load_settings())
    with TestClient(app) as test_client:
        yield test_client


def test_new_image_file_is_processed_and_thumb_created(client: TestClient, source_root: Path) -> None:
    image_path = source_root / "new-image.jpg"
    create_image(image_path)

    _, second_scan = scan_twice(client)

    media_response = client.get("/media").json()
    assert second_scan["job"]["result"]["summary"]["created"] == 1
    assert media_response["total"] == 1

    item = media_response["items"][0]
    detail = client.get(f"/media/{item['file_id']}").json()

    assert item["media_kind"] == "image"
    assert item["status"] == "thumb_done"
    assert [asset["asset_kind"] for asset in detail["derived_assets"]] == ["thumb"]


def test_new_video_file_is_ignored(client: TestClient, source_root: Path) -> None:
    video_path = source_root / "new-video.mp4"
    create_video(video_path)

    _, second_scan = scan_twice(client)

    media_response = client.get("/media").json()
    assert second_scan["job"]["result"]["summary"]["created"] == 0
    assert media_response["total"] == 0


def test_moved_file_keeps_same_file_id_and_updates_path(client: TestClient, source_root: Path) -> None:
    original_path = source_root / "before-move.jpg"
    moved_path = source_root / "after-move.jpg"
    create_image(original_path)

    scan_twice(client)
    first_item = client.get("/media").json()["items"][0]

    original_path.rename(moved_path)
    scan_twice(client)

    media_response = client.get("/media").json()
    assert media_response["total"] == 1

    moved_item = media_response["items"][0]
    assert moved_item["file_id"] == first_item["file_id"]
    assert moved_item["current_path"] == str(moved_path)


def test_deleted_file_is_marked_missing_when_scan_yield_is_not_low(client: TestClient, source_root: Path) -> None:
    image_path = source_root / "delete-me.jpg"
    create_image(image_path)
    for index in range(100):
        create_image(source_root / f"keep-{index:03d}.jpg")

    scan_twice(client)
    image_path.unlink()

    client.post("/scan")
    missing_response = client.get("/media", params={"status": "missing"}).json()

    assert missing_response["total"] == 1
    assert missing_response["items"][0]["status"] == "missing"


def test_partial_upload_stays_waiting_stable_when_size_changes(client: TestClient, source_root: Path) -> None:
    image_path = source_root / "partial-upload.jpg"
    full_payload = build_image_bytes()
    image_path.write_bytes(full_payload[: len(full_payload) // 2])

    client.post("/scan")
    time.sleep(SCAN_DELAY_SECONDS)
    image_path.write_bytes(full_payload)
    client.post("/scan")

    status_response = client.get("/status").json()
    media_response = client.get("/media").json()

    assert status_response["catalog"]["waiting_stable"] == 1
    assert media_response["total"] == 0


def test_nas_offline_keeps_existing_metadata_available(client: TestClient, source_root: Path, tmp_path: Path) -> None:
    image_path = source_root / "offline-check.jpg"
    create_image(image_path)

    scan_twice(client)
    before = client.get("/media").json()
    offline_root = tmp_path / "offline-source"
    source_root.rename(offline_root)

    client.post("/scan")
    after = client.get("/media").json()

    assert before["total"] == 1
    assert after["total"] == 1
    assert after["items"][0]["status"] != "missing"


def test_filter_by_media_type_returns_only_requested_type(client: TestClient, source_root: Path) -> None:
    create_image(source_root / "only-image.jpg")
    create_video(source_root / "only-video.mp4")

    scan_twice(client)

    images = client.get("/media/filter", params={"media_kind": "image"}).json()
    videos = client.get("/media/filter", params={"media_kind": "video"}).json()

    assert images["total"] == 1
    assert videos["total"] == 0
    assert all(item["media_kind"] == "image" for item in images["items"])


def scan_twice(client: TestClient) -> tuple[dict, dict]:
    first = client.post("/scan").json()
    time.sleep(SCAN_DELAY_SECONDS)
    second = client.post("/scan").json()
    return first, second


def create_image(path: Path, *, color: tuple[int, int, int] = (40, 80, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (64, 48), color=color)
    image.save(path, format="JPEG")


def build_image_bytes() -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", (64, 48), color=(120, 40, 80))
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def create_video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for video scenario tests")

    command = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=1:duration=1",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "failed to create test video")
