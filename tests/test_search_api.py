from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy import func, select, text

from app.core.settings import load_settings
from app.db.bootstrap import build_database_state
from app.main import create_app
from app.models.asset import DerivedAsset
from app.models.job import ProcessingJob
from app.models.media import MediaFile
from app.models.person import Person
from app.models.face import Face
from app.models.runtime import SchedulerRuntimeConfig
from app.core.contracts import MediaTagInput
from app.models.semantic import MediaAnalysisSignal, MediaEmbedding, MediaOCR, SearchDocument, SearchEvent, SearchFeedback
from app.models.tag import Tag
from app.services.caption.registry import get_caption_provider
from app.services.processing.incremental import IncrementalScanSummary


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
    database_path = data_root / "photome.sqlite3"

    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("TROVE_STABILITY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TROVE_FACE_ANALYSIS_ENABLED", "0")
    monkeypatch.setenv("TROVE_CLIP_ENABLED", "0")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    app = create_app(load_settings())
    with TestClient(app) as test_client:
        yield test_client


def test_search_finds_scanned_media_by_filename_and_semantic_rows_exist(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "vacation-receipt.jpg")
    scan_twice(client)

    response = client.get("/search", params={"q": "receipt"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["security"]["runtime_mode"] in {"standard", "offline-local-only"}
    assert payload["items"][0]["filename"] == "vacation-receipt.jpg"

    file_id = payload["items"][0]["file_id"]
    with client.app.state.database.session_factory() as session:
        assert session.get(MediaOCR, file_id) is not None
        assert session.get(MediaAnalysisSignal, file_id) is not None
        search_document = session.get(SearchDocument, file_id)
        assert search_document is not None
        assert "vacation-receipt.jpg" in search_document.search_text
        assert search_document.version == client.app.state.settings.semantic_search_version
        indexed = session.execute(
            text("SELECT file_id FROM search_documents_fts WHERE search_documents_fts MATCH 'receipt'")
        ).all()
        assert indexed == [(file_id,)]


def test_search_tolerates_event_commit_failure(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_image(source_root / "commit-failure-receipt.jpg")
    scan_twice(client)

    original_commit = client.app.state.database.session_factory.class_.commit

    def flaky_commit(session):  # type: ignore[no-untyped-def]
        raise PendingRollbackError("locked session")

    monkeypatch.setattr(client.app.state.database.session_factory.class_, "commit", flaky_commit)
    try:
        response = client.get("/search", params={"q": "receipt"})
    finally:
        monkeypatch.setattr(client.app.state.database.session_factory.class_, "commit", original_commit)

    assert response.status_code == 200
    assert response.json()["total"] >= 1


def test_offline_mode_disables_outbound_features(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photome.sqlite3"
    source_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("TROVE_OFFLINE_MODE", "1")
    monkeypatch.setenv("TROVE_CAPTION_PROVIDER", "moondream")
    monkeypatch.setenv("TROVE_FACE_ANALYSIS_ENABLED", "1")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    settings = load_settings()
    assert settings.offline_mode is True
    assert get_caption_provider() is None

    app = create_app(settings)
    with TestClient(app) as test_client:
        response = test_client.get("/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["security"]["offline_mode"] is True
        assert payload["security"]["outbound_network_enabled"] is False
        assert payload["security"]["runtime_mode"] == "offline-local-only"
        assert payload["security"]["deployment_mode"] in {"native", "docker"}
        assert payload["security"]["deployment_label"] in {"Native local process", "Docker container"}
        assert any("geocoding" in f.lower() for f in payload["security"]["disabled_features"])
        assert "Caption generation is disabled." in payload["security"]["disabled_features"]
        states = {item["name"]: item["state"] for item in payload["security"]["local_dependencies"]}
        assert "ffmpeg" in states
        assert "ffprobe" in states
        assert states["CLIP semantic embedding"] == "disabled"
        assert states["Caption provider"] == "disabled"
        prepare = test_client.post("/ai-pack/prepare")
        assert prepare.status_code == 409
        assert "Offline mode blocks automatic model downloads" in prepare.json()["message"]
        assert "load_cached=true" in prepare.json()["message"]


def test_clip_status_degrades_when_local_ai_pack_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photome.sqlite3"
    source_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("TROVE_CLIP_ENABLED", "1")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    from app.services.embedding import clip as clip_embedding

    monkeypatch.setattr(
        clip_embedding,
        "dependency_status",
        lambda: {"open_clip_torch": "missing", "torch": "missing", "torchvision": "missing"},
    )

    app = create_app(load_settings())
    with TestClient(app) as test_client:
        response = test_client.get("/status")

    assert response.status_code == 200
    states = {item["name"]: item for item in response.json()["security"]["local_dependencies"]}
    clip_state = states["CLIP semantic embedding"]
    assert clip_state["state"] == "missing-local-ai-pack"
    assert clip_state["dependencies"]["open_clip_torch"] == "missing"


def test_semantic_maintenance_only_builds_missing_search_documents(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "cycle-receipt.jpg")
    scan_twice(client)
    item = client.get("/search", params={"q": "receipt"}).json()["items"][0]

    no_op = client.app.state.pipeline.run_semantic_maintenance()
    assert no_op["pending"] == 0
    assert no_op["succeeded"] == 0

    with client.app.state.database.session_factory() as session:
        document = session.get(SearchDocument, item["file_id"])
        assert document is not None
        session.delete(document)
        session.commit()

    rebuilt = client.app.state.pipeline.run_semantic_maintenance()
    assert rebuilt["pending"] == 1
    assert rebuilt["succeeded"] == 1


def test_status_reports_phase2_coverage(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "coverage-receipt.jpg")
    scan_twice(client)

    payload = client.get("/status").json()
    coverage = payload["semantic"]["coverage"]

    assert coverage["eligible_media"] >= 1
    assert coverage["search_current"] >= 1
    assert coverage["remaining_for_search"] >= 0
    # 메뉴바/설정 페이지 공용 단일 지표: 완료 + 남음 = 전체가 항상 성립해야 한다.
    assert coverage["analyzed_current"] + coverage["remaining_for_analysis"] == coverage["eligible_media"]
    assert coverage["analyzed_current"] >= 1
    assert "clip_embeddings_current" in coverage
    assert "semantic_job_errors" in coverage
    assert payload["catalog"]["breakdown"]["summary_text"].startswith("1. 토탈 ")
    assert "4. 미해당" in payload["catalog"]["breakdown"]["summary_text"]
    assert "5. 오류" in payload["catalog"]["breakdown"]["summary_text"]
    ai_payload = payload["performance"]["ai_summary"]
    assert ai_payload["remaining_clip"] == max(0, ai_payload["eligible_media"] - ai_payload["clip_embeddings"])
    assert ai_payload["summary_text"].startswith("1. 대상 ")
    assert "4. 미해당" in ai_payload["summary_text"]
    assert "5. 오류" in ai_payload["summary_text"]
    assert any("CLIP 임베딩 상태" in note for note in ai_payload["notes"])


def test_status_detail_files_and_ai_include_summary_notes(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "status-detail.jpg")
    scan_twice(client)

    files_detail = client.get("/status/detail/files")
    assert files_detail.status_code == 200
    files_payload = files_detail.json()
    assert files_payload["summary"][0]["label"] == "1. 토탈"
    assert len(files_payload["summary"]) == 5
    assert any("완료:" in note for note in files_payload["notes"])
    assert any("미해당" in item["label"] for item in files_payload["summary"])

    ai_detail = client.get("/status/detail/ai")
    assert ai_detail.status_code == 200
    ai_payload = ai_detail.json()
    assert ai_payload["summary"][0]["label"] == "1. 대상"
    assert len(ai_payload["summary"]) == 5
    assert any("CLIP 임베딩 상태" in note for note in ai_payload["notes"])


def test_semantic_maintenance_fills_missing_clip_embeddings_when_enabled(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_image(source_root / "plain-photo.jpg")
    scan_twice(client)

    pipeline = client.app.state.pipeline
    pipeline._semantic_clip_enabled = True

    def fake_embedding(media_file: MediaFile) -> dict:
        return {
            "model_name": "ViT-B-32/openai",
            "version": pipeline._semantic_embedding_version,
            "embedding_ref": f"embeddings/clip/{pipeline._semantic_embedding_version}/aa/{media_file.file_id}.npy",
            "dimensions": 3,
            "checksum": None,
        }

    monkeypatch.setattr(pipeline, "_materialize_clip_embedding", fake_embedding)
    from app.services.analysis import auto_tags

    monkeypatch.setattr(
        auto_tags,
        "tags_from_embedding_file",
        lambda *_args, **_kwargs: [MediaTagInput(tag_type="auto", tag_value="바다")],
    )

    result = pipeline.run_semantic_maintenance(batch_size=10)

    assert result["succeeded"] >= 1
    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "plain-photo.jpg"))
        assert media_file is not None
        assert session.scalar(select(MediaEmbedding).where(MediaEmbedding.file_id == media_file.file_id)) is not None
        assert session.scalar(
            select(func.count())
            .select_from(Tag)
            .where(Tag.file_id == media_file.file_id, Tag.tag_type == "auto", Tag.tag_value == "바다")
        ) == 1
        search_document = session.get(SearchDocument, media_file.file_id)
        assert search_document is not None
        assert "바다" in search_document.search_text

    second_no_op = client.post("/scan/semantic-maintenance").json()
    assert second_no_op["pending"] == 0


def test_semantic_maintenance_prioritizes_missing_clip_embeddings(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """임베딩 누락분이 검색문서/태그 갱신 백로그보다 먼저 배치에 들어간다."""
    create_image(source_root / "tag-refresh-backlog.jpg")
    create_image(source_root / "needs-embedding.jpg")
    scan_twice(client)

    pipeline = client.app.state.pipeline
    pipeline._semantic_clip_enabled = True

    def fake_embedding(media_file: MediaFile) -> dict:
        return {
            "model_name": "ViT-B-32/openai",
            "version": pipeline._semantic_embedding_version,
            "embedding_ref": f"embeddings/clip/{pipeline._semantic_embedding_version}/aa/{media_file.file_id}.npy",
            "dimensions": 3,
            "checksum": None,
        }

    monkeypatch.setattr(pipeline, "_materialize_clip_embedding", fake_embedding)
    from app.services.analysis import auto_tags

    monkeypatch.setattr(auto_tags, "tags_from_embedding_file", lambda *_args, **_kwargs: [])

    with client.app.state.database.session_factory() as session:
        backlog = session.scalar(select(MediaFile).where(MediaFile.filename == "tag-refresh-backlog.jpg"))
        target = session.scalar(select(MediaFile).where(MediaFile.filename == "needs-embedding.jpg"))
        assert backlog is not None and target is not None
        # backlog는 임베딩이 이미 있고 검색문서 갱신만 남은 상태로 만든다.
        session.add(
            MediaEmbedding(
                file_id=backlog.file_id,
                model_name="ViT-B-32/openai",
                version=pipeline._semantic_embedding_version,
                embedding_ref="embeddings/clip/test/backlog.npy",
                dimensions=3,
            )
        )
        document = session.get(SearchDocument, backlog.file_id)
        assert document is not None
        session.delete(document)
        session.commit()
        backlog_id = backlog.file_id
        target_id = target.file_id

    result = pipeline.run_semantic_maintenance(batch_size=1)

    assert result["pending"] == 1
    assert result["embeddings_created"] == 1
    with client.app.state.database.session_factory() as session:
        assert (
            session.scalar(select(MediaEmbedding).where(MediaEmbedding.file_id == target_id)) is not None
        )
        # 검색문서 백로그는 다음 배치로 밀린다.
        assert session.get(SearchDocument, backlog_id) is None


def test_clip_embedding_reuse_requires_matching_model_name(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_image(source_root / "model-change.jpg")
    scan_twice(client)

    pipeline = client.app.state.pipeline
    pipeline._semantic_clip_enabled = True
    calls = {"count": 0}

    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "model-change.jpg"))
        assert media_file is not None
        session.add(
            MediaEmbedding(
                file_id=media_file.file_id,
                model_name="old-model/old-pretrained",
                version=pipeline._semantic_embedding_version,
                embedding_ref=f"embeddings/clip/{pipeline._semantic_embedding_version}/old/{media_file.file_id}.npy",
                dimensions=3,
            )
        )
        session.commit()

    monkeypatch.setenv("TROVE_CLIP_MODEL_NAME", "new-model")
    monkeypatch.setenv("TROVE_CLIP_PRETRAINED", "new-pretrained")

    def fake_embedding(media_file: MediaFile) -> dict:
        calls["count"] += 1
        return {
            "model_name": pipeline._clip_model_identifier(),
            "version": pipeline._semantic_embedding_version,
            "embedding_ref": f"embeddings/clip/{pipeline._semantic_embedding_version}/new/{media_file.file_id}.npy",
            "dimensions": 3,
            "checksum": None,
        }

    monkeypatch.setattr(pipeline, "_materialize_clip_embedding", fake_embedding)

    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "model-change.jpg"))
        assert media_file is not None
        from app.services.processing.registry import MediaCatalog
        from app.services.semantic import SemanticCatalog

        result = pipeline._ensure_clip_embedding(
            session,
            media_file,
            MediaCatalog(session),
            SemanticCatalog(session),
        )
        session.commit()

    assert calls["count"] == 1
    assert result is not None
    assert result["model_name"] == "new-model/new-pretrained"
    with client.app.state.database.session_factory() as session:
        rows = session.scalars(select(MediaEmbedding).where(MediaEmbedding.file_id == media_file.file_id)).all()
        assert {row.model_name for row in rows} == {"old-model/old-pretrained", "new-model/new-pretrained"}


def test_embedding_pending_uses_current_model_and_version(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_image(source_root / "pending-model-change.jpg")
    scan_twice(client)

    pipeline = client.app.state.pipeline
    pipeline._semantic_clip_enabled = True

    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "pending-model-change.jpg"))
        assert media_file is not None
        session.add(
            MediaEmbedding(
                file_id=media_file.file_id,
                model_name="old-model/old-pretrained",
                version=pipeline._semantic_embedding_version,
                embedding_ref=f"embeddings/clip/{pipeline._semantic_embedding_version}/old/{media_file.file_id}.npy",
                dimensions=3,
            )
        )
        session.commit()

    monkeypatch.setenv("TROVE_CLIP_MODEL_NAME", "new-model")
    monkeypatch.setenv("TROVE_CLIP_PRETRAINED", "new-pretrained")
    calls = {"count": 0}

    def fake_embedding(media_file: MediaFile) -> dict:
        calls["count"] += 1
        return {
            "model_name": pipeline._clip_model_identifier(),
            "version": pipeline._semantic_embedding_version,
            "embedding_ref": f"embeddings/clip/{pipeline._semantic_embedding_version}/new/{media_file.file_id}.npy",
            "dimensions": 3,
            "checksum": None,
        }

    monkeypatch.setattr(pipeline, "_materialize_clip_embedding", fake_embedding)

    result = pipeline.run_semantic_maintenance(batch_size=10)

    assert result["succeeded"] >= 1
    assert calls["count"] == 1


def test_search_event_is_persisted_after_search(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "event-receipt.jpg")
    scan_twice(client)

    response = client.get("/search", params={"q": "receipt"})
    cached_response = client.get("/search", params={"q": "receipt"})

    assert response.status_code == 200
    assert cached_response.status_code == 200
    with client.app.state.database.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(SearchEvent))
        assert count == 2


def test_search_event_is_skipped_while_library_job_is_active(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_image(source_root / "active-job-receipt.jpg")
    scan_twice(client)

    monkeypatch.setattr(client.app.state.pipeline, "has_active_library_job", lambda: True)
    response = client.get("/search", params={"q": "receipt"})

    assert response.status_code == 200
    with client.app.state.database.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(SearchEvent))
        assert count == 0


def test_date_fallback_does_not_recurse_on_zero_results(client: TestClient) -> None:
    response = client.get("/search", params={"q": "2099년 12월 31일 qqqzzznotfound"})

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_clip_disabled_search_does_not_load_clip_model(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.embedding import clip as clip_embedding

    called = False

    def mark_called() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(clip_embedding, "ensure_models", mark_called)

    response = client.get("/search", params={"q": "얼굴"})

    assert response.status_code == 200
    assert called is False


def test_feedback_invalidates_cached_search_results(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "cached-receipt.jpg")
    scan_twice(client)

    first = client.get("/search", params={"q": "receipt"}).json()
    assert first["total"] == 1
    file_id = first["items"][0]["file_id"]

    feedback = client.post(
        "/search/feedback",
        json={"file_id": file_id, "action": "hide"},
    )
    assert feedback.status_code == 201

    second = client.get("/search", params={"q": "receipt"}).json()
    assert second["total"] == 0


def test_weight_profile_rejects_invalid_values(client: TestClient) -> None:
    negative = client.put(
        "/search/weights/hybrid/fallback",
        json={"w_ocr": -1, "w_clip": 0.5, "w_shadow": 0.5},
    )
    assert negative.status_code == 422

    zero_total = client.put(
        "/search/weights/hybrid/fallback",
        json={"w_ocr": 0, "w_clip": 0, "w_shadow": 0},
    )
    assert zero_total.status_code == 422


def test_media_annotation_updates_display_name_description_and_custom_tags(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "vacation-receipt.jpg")
    scan_twice(client)
    item = client.get("/media", params={"q": "receipt"}).json()["items"][0]

    response = client.post(
        f"/media/{item['file_id']}/annotation",
        data={
            "title": "Trip receipt",
            "description": "Dinner receipt from the family trip.",
            "tags": "receipt, family, receipt",
            "next": "/gallery?q=receipt",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail = client.get(f"/media/{item['file_id']}").json()
    assert detail["annotation"] == {
        "title": "Trip receipt",
        "description": "Dinner receipt from the family trip.",
    }
    assert {
        (tag["tag_type"], tag["tag_value"])
        for tag in detail["tags"]
        if tag["tag_type"] == "custom"
    } == {("custom", "receipt"), ("custom", "family")}

    # 갤러리 카드/라이트박스 간소화(31e5a71) 이후 설명 텍스트는 HTML에 노출되지
    # 않는다. 제목은 라이트박스 캡션/aria-label로 남고, 설명은 검색으로 검증한다.
    gallery = client.get("/gallery", params={"q": "receipt"}).text
    assert "Trip receipt" in gallery

    title_search = client.get("/search", params={"q": "Trip"})
    assert title_search.status_code == 200
    assert title_search.json()["items"][0]["file_id"] == item["file_id"]

    gallery_title_search = client.get("/gallery", params={"q": "Trip"}).text
    assert "Trip receipt" in gallery_title_search
    assert f"card-{item['file_id']}" in gallery_title_search


def test_gallery_recovers_from_transient_thumbnail_load_failures(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "vacation-receipt.jpg")
    scan_twice(client)

    gallery = client.get("/gallery").text

    assert "attachThumbnailRecovery" in gallery
    assert "미리보기 준비 중" in gallery


def test_gallery_query_preserves_search_ranking(
    client: TestClient,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_image(source_root / "older-ranked-first.jpg")
    create_image(source_root / "newer-ranked-second.jpg")
    scan_twice(client)

    with client.app.state.database.session_factory() as session:
        older = session.scalar(select(MediaFile).where(MediaFile.filename == "older-ranked-first.jpg"))
        newer = session.scalar(select(MediaFile).where(MediaFile.filename == "newer-ranked-second.jpg"))
        assert older is not None
        assert newer is not None
        older.exif_datetime = datetime(2020, 1, 1, 12, 0, 0)
        newer.exif_datetime = datetime(2025, 1, 1, 12, 0, 0)
        older_id = older.file_id
        newer_id = newer.file_id
        session.commit()

    def fake_search_with_meta(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return [{"file_id": older_id}, {"file_id": newer_id}], {"intent_reason": "manual"}

    import app.api.gallery as gallery_api

    monkeypatch.setattr(gallery_api.HybridSearchService, "search_with_meta", fake_search_with_meta)

    gallery = client.get("/gallery", params={"q": "동이"}).text

    # Gallery now sorts by date (newest first) within the search-matched set,
    # so the 2025 photo appears before the 2020 photo regardless of RRF rank order.
    assert gallery.index("newer-ranked-second.jpg") < gallery.index("older-ranked-first.jpg")


def test_gallery_defaults_to_file_mtime_when_exif_datetime_is_missing(
    client: TestClient,
    source_root: Path,
) -> None:
    older = source_root / "older-no-exif.png"
    newer = source_root / "newer-no-exif.png"
    create_image(older)
    create_image(newer)
    scan_twice(client)

    older_ts = datetime(2020, 1, 1, 12, 0, 0).timestamp()
    newer_ts = datetime(2024, 1, 1, 12, 0, 0).timestamp()

    with client.app.state.database.session_factory() as session:
        older_row = session.scalar(select(MediaFile).where(MediaFile.filename == "older-no-exif.png"))
        newer_row = session.scalar(select(MediaFile).where(MediaFile.filename == "newer-no-exif.png"))
        assert older_row is not None
        assert newer_row is not None
        older_row.exif_datetime = None
        newer_row.exif_datetime = None
        older_row.mtime_ns = int(older_ts * 1_000_000_000)
        newer_row.mtime_ns = int(newer_ts * 1_000_000_000)
        older_row.processed_at = datetime(2026, 5, 12, 6, 10, 34)
        newer_row.processed_at = datetime(2026, 5, 12, 6, 10, 34)
        session.commit()

    gallery = client.get("/gallery").text

    # 카드에 날짜 텍스트는 더 이상 노출되지 않으므로(31e5a71 간소화),
    # mtime 폴백은 최신순 정렬 결과로 검증한다.
    assert gallery.index("newer-no-exif.png") < gallery.index("older-no-exif.png")


def test_gallery_asset_missing_response_is_not_cached(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "vacation-receipt.jpg")
    scan_twice(client)

    gallery = client.get("/gallery").text
    match = re.search(r'/gallery/assets/(\d+)', gallery)
    assert match is not None
    asset_id = match.group(1)
    with client.app.state.database.session_factory() as session:
        asset = session.get(DerivedAsset, int(asset_id))
        assert asset is not None
        asset_path = client.app.state.settings.derived_root / asset.derived_path
        asset_path.unlink()

    response = client.get(f"/gallery/assets/{asset_id}")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_gallery_asset_rejects_absolute_path_outside_derived_root(
    client: TestClient,
    source_root: Path,
    tmp_path: Path,
) -> None:
    create_image(source_root / "vacation-receipt.jpg")
    scan_twice(client)

    with client.app.state.database.session_factory() as session:
        asset = session.scalars(select(DerivedAsset).limit(1)).first()
        assert asset is not None
        outside_file = tmp_path / "outside.jpg"
        outside_file.write_bytes(b"not a derived asset")
        asset.derived_path = str(outside_file)
        session.commit()
        asset_id = asset.id

    response = client.get(f"/gallery/assets/{asset_id}")

    assert response.status_code == 404


def test_scan_accepts_source_roots_query_override(client: TestClient, tmp_path: Path) -> None:
    selected_root = tmp_path / "selected-source"
    selected_root.mkdir()
    create_image(selected_root / "manual-path-receipt.jpg")

    client.post("/scan", params={"source_roots": str(selected_root)})
    time.sleep(SCAN_DELAY_SECONDS)
    response = client.post("/scan", params={"source_roots": str(selected_root)})

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["payload"]["source_roots"] == [str(selected_root.resolve())]
    assert job["result"]["source_roots"] == [str(selected_root.resolve())]
    assert job["result"]["summary"]["created"] == 1

    search = client.get("/search", params={"q": "receipt"})
    assert search.status_code == 200
    assert search.json()["total"] == 1


def test_async_scan_starts_job_and_exposes_status(client: TestClient, tmp_path: Path) -> None:
    selected_root = tmp_path / "async-source"
    selected_root.mkdir()
    create_image(selected_root / "async-receipt.jpg")
    client.post("/scan", params={"source_roots": str(selected_root)})
    time.sleep(SCAN_DELAY_SECONDS)

    response = client.post("/scan/async", params={"source_roots": str(selected_root)})

    assert response.status_code == 202
    job = response.json()["job"]
    assert job["status"] in {"queued", "succeeded"}
    status_response = client.get(f"/scan/jobs/{job['job_id']}")
    assert status_response.status_code == 200
    status_job = status_response.json()["job"]
    assert status_job["job_id"] == job["job_id"]
    assert status_job["status"] == "succeeded"
    assert status_job["result"]["summary"]["created"] == 1


def test_scan_accepts_host_path_and_maps_it_to_docker_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.api.scan as scan_api

    host_root = tmp_path / "Volumes" / "homes" / "dejeong" / "Photos"
    mount_root = tmp_path / "mounted-photos"
    host_album = host_root / "album"
    mount_album = mount_root / "album"
    host_album.mkdir(parents=True, exist_ok=True)
    mount_album.mkdir(parents=True, exist_ok=True)
    create_image(mount_album / "mapped-receipt.jpg")

    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photome.sqlite3"

    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(mount_root))
    monkeypatch.setenv("TROVE_SOURCE_ROOT_HOST", str(host_root))
    monkeypatch.setenv("TROVE_SOURCE_ROOT_MOUNT", str(mount_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("TROVE_STABILITY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TROVE_FACE_ANALYSIS_ENABLED", "0")
    monkeypatch.setenv("TROVE_CLIP_ENABLED", "0")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    original_exists = scan_api.os.path.exists

    def fake_exists(path: str) -> bool:
        if path == "/.dockerenv":
            return True
        return original_exists(path)

    monkeypatch.setattr(scan_api.os.path, "exists", fake_exists)

    app = create_app(load_settings())
    with TestClient(app) as mapped_client:
        response = mapped_client.post("/scan", params={"source_roots": str(host_album)})
        assert response.status_code == 200
        job = response.json()["job"]
        assert job["payload"]["source_roots"] == [str(mount_album.resolve())]
        assert job["result"]["source_roots"] == [str(mount_album.resolve())]

        dashboard = mapped_client.get("/dashboard").text
        assert str(host_root) in dashboard


def test_scan_rejects_unmounted_explicit_docker_path_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.api.scan as scan_api

    configured_root = tmp_path / "configured-source"
    configured_root.mkdir(parents=True, exist_ok=True)
    create_image(configured_root / "should-not-be-scanned.jpg")
    missing_finder_root = tmp_path / "Volumes" / "nas" / "MissingPhotos"

    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photome.sqlite3"

    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(configured_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("TROVE_STABILITY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TROVE_FACE_ANALYSIS_ENABLED", "0")
    monkeypatch.setenv("TROVE_CLIP_ENABLED", "0")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    original_exists = scan_api.os.path.exists

    def fake_exists(path: str) -> bool:
        if path == "/.dockerenv":
            return True
        return original_exists(path)

    monkeypatch.setattr(scan_api.os.path, "exists", fake_exists)

    app = create_app(load_settings())
    with TestClient(app) as docker_client:
        response = docker_client.post("/scan", params={"source_roots": str(missing_finder_root)})
        assert response.status_code == 400
        assert "Source root does not exist" in response.json()["detail"]

        dashboard = docker_client.get("/dashboard").text
        assert "should-not-be-scanned.jpg" not in dashboard


def test_full_scan_imports_old_archive_files_on_first_pass(client: TestClient, tmp_path: Path) -> None:
    nested_root = tmp_path / "archive-source"
    nested_file = nested_root / "album-a" / "album-b" / "archive-receipt.jpg"
    nested_file.parent.mkdir(parents=True, exist_ok=True)
    create_image(nested_file)
    old_timestamp = time.time() - 600
    os.utime(nested_file, (old_timestamp, old_timestamp))

    response = client.post(
        "/scan",
        params={"full_scan": "true", "source_roots": str(nested_root)},
    )

    assert response.status_code == 200
    summary = response.json()["job"]["result"]["summary"]
    assert summary["scanned"] == 1
    assert summary["created"] == 1

    search = client.get("/search", params={"q": "archive"})
    assert search.status_code == 200
    assert search.json()["total"] == 1


def test_video_files_are_ignored_by_library_scan(
    client: TestClient,
    source_root: Path,
) -> None:
    video_path = source_root / "offline-video.mp4"
    video_path.write_bytes(b"fake mp4 bytes")

    _, second = scan_twice(client)

    assert second["job"]["status"] == "succeeded"
    assert second["job"]["result"]["processed"]["failed"] == 0
    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "offline-video.mp4"))
        assert media_file is None


def test_existing_video_errors_are_excluded_on_next_scan(
    client: TestClient,
    source_root: Path,
) -> None:
    now = datetime.utcnow()
    with client.app.state.database.session_factory() as session:
        session.add(
            MediaFile(
                file_id="video-error-1",
                current_path=str(source_root / "old-video.mp4"),
                filename="old-video.mp4",
                source_root=str(source_root),
                relative_path="old-video.mp4",
                media_kind="video",
                status="error",
                size_bytes=123,
                mtime_ns=123,
                partial_hash="abc",
                fingerprint_version="v1",
                error_stage="asset_pipeline",
                error_message="ffmpeg is required",
                error_count=5,
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        session.commit()

    _, second = scan_twice(client)

    assert second["job"]["status"] == "succeeded"
    with client.app.state.database.session_factory() as session:
        media_file = session.get(MediaFile, "video-error-1")
        assert media_file is not None
        assert media_file.status == "excluded"
        assert media_file.error_stage is None
        assert media_file.error_message is None


def test_missing_image_source_is_demoted_to_missing_in_asset_pass(
    client: TestClient,
    source_root: Path,
) -> None:
    now = datetime.utcnow()
    with client.app.state.database.session_factory() as session:
        session.add(
            MediaFile(
                file_id="missing-image-1",
                current_path=str(source_root / "gone.jpg"),
                filename="gone.jpg",
                source_root=str(source_root),
                relative_path="gone.jpg",
                media_kind="image",
                status="error",
                size_bytes=123,
                mtime_ns=123,
                partial_hash="abc",
                fingerprint_version="v1",
                error_stage="asset_pipeline",
                error_message="old missing file error",
                error_count=2,
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        session.commit()

    response = client.post("/scan/retry-errors/async")

    assert response.status_code == 202
    with client.app.state.database.session_factory() as session:
        media_file = session.get(MediaFile, "missing-image-1")
        assert media_file is not None
        assert media_file.status == "missing"


def test_async_semantic_maintenance_job_exposes_status(client: TestClient, source_root: Path) -> None:
    create_image(source_root / "semantic-job-receipt.jpg")
    scan_twice(client)

    response = client.post("/scan/semantic-maintenance/async", params={"batch_size": 10})

    assert response.status_code == 202
    job = response.json()["job"]
    status_response = client.get(f"/scan/jobs/{job['job_id']}")
    assert status_response.status_code == 200
    status_job = status_response.json()["job"]
    assert status_job["job_id"] == job["job_id"]
    assert status_job["status"] == "succeeded"
    assert "pending" in status_job["result"]


def test_full_scan_reprocesses_present_missing_media(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "was-missing.jpg")
    scan_twice(client)

    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "was-missing.jpg"))
        assert media_file is not None
        media_file.status = "missing"
        media_file.error_stage = None
        media_file.error_message = None
        file_id = media_file.file_id
        session.commit()

    response = client.post("/scan/async")

    assert response.status_code == 202
    job = response.json()["job"]
    status_response = client.get(f"/scan/jobs/{job['job_id']}")
    assert status_response.status_code == 200
    completed = status_response.json()["job"]
    assert completed["status"] == "succeeded"
    assert completed["result"]["processed"]["succeeded"] >= 1

    with client.app.state.database.session_factory() as session:
        retried = session.get(MediaFile, file_id)
        assert retried is not None
        assert retried.status in {"thumb_done", "analysis_done"}


def test_scan_retry_errors_endpoint_reprocesses_failed_media(
    client: TestClient,
    source_root: Path,
) -> None:
    create_image(source_root / "retry-me.jpg")
    scan_twice(client)

    with client.app.state.database.session_factory() as session:
        media_file = session.scalar(select(MediaFile).where(MediaFile.filename == "retry-me.jpg"))
        assert media_file is not None
        media_file.status = "error"
        media_file.error_stage = "asset_pipeline"
        media_file.error_message = "temporary failure"
        file_id = media_file.file_id
        session.commit()

    response = client.post("/scan/retry-errors/async")

    assert response.status_code == 202
    job = response.json()["job"]
    assert job["payload"]["retry_errors_only"] is True

    status_response = client.get(f"/scan/jobs/{job['job_id']}")
    assert status_response.status_code == 200
    completed = status_response.json()["job"]
    assert completed["status"] == "succeeded"
    assert completed["result"]["retry_errors_only"] is True
    assert completed["result"]["processed"]["succeeded"] >= 1

    with client.app.state.database.session_factory() as session:
        retried = session.get(MediaFile, file_id)
        assert retried is not None
        assert retried.status in {"thumb_done", "analysis_done"}
        assert retried.error_stage is None
        assert retried.error_message is None


def test_async_job_dashboard_restores_phase_cards_from_local_storage(client: TestClient) -> None:
    response = client.get("/dashboard")

    assert response.status_code == 200
    html = response.text
    assert "Network" in html
    assert "Deployment" in html
    assert "Configured source roots" in html
    assert "Cataloged source roots" in html
    assert "라이브러리 동기화" in html
    assert "openDetailPopup('files','파일 현황 기준')" in html
    assert "1. 토탈" in html
    assert "4. 미해당" in html
    assert "5. 오류" in html
    assert "1. 대상" in html
    assert 'id="m-ai-state"' in html
    assert "function updateAiMetricState(payload, phase1OwnsActive, phase2OwnsActive)" in html
    assert "다음 동기화에서 이어서 처리" in html
    # 통합 동기화: 별도 유휴 이미지 AI 스케줄/백그라운드 작업 표기는 없어야 한다.
    assert "background_task" not in html
    assert "자동 동기화" in html
    assert 'const phase1StorageKey = "trove.dashboard.phase1.job";' in html
    # phase2 카드 제거(B-8): 관련 JS/스토리지 키도 함께 사라져야 한다.
    assert "phase2StorageKey" not in html
    assert 'id="phase2-card"' not in html
    assert 'const phase1SourceRootsStorageKey = "trove.dashboard.phase1.source_roots";' in html
    assert "let activeLibraryJob =" in html
    assert "function updateLibraryJobGuards()" in html
    assert "setInterval(refreshDashboardStatus, 3000);" in html
    assert 'id="phase1-schedule-button"' in html
    assert 'id="phase2-schedule-button"' not in html  # removed: unified into library schedule
    assert 'id="phase1-retry-button"' in html
    # 설정 탭 다이어트: 시작 버튼/진행 수치는 메뉴바로 이동, 설정성 요소만 남는다.
    assert "전체 동기화 시작" not in html
    assert 'id="phase1-scan-button"' not in html
    assert 'id="p1-pending"' not in html
    assert "지금 동기화" in html
    assert "오류 항목만 재처리" in html
    assert "경로 구분: Finder 저장장치는" in html
    assert "Docker 내부 사진 폴더(/photos)는 환경변수로 따로 붙인 호환용 경로" in html
    assert "System Tools" in html
    assert 'id="search-inspector-card"' in html
    assert "Developer tool for checking search routing" in html
    assert "로컬 모델 캐시 사용" in html or "모델" in html
    assert "Load cached model" in html or "로컬 캐시 확인" in html
    assert "/ai-pack/prepare?load_cached=true" in html
    assert "python -m app.main" in html or "docker compose --env-file .env.docker.example up -d trove" in html
    assert 'id="phase1-full-scan"' not in html
    assert "function formatElapsed(startedAt, finishedAt)" in html
    assert "async function pollJob(jobId, resultNode, render)" in html
    assert "if (progress.message) lines.push(progress.message);" in html
    assert "function renderLibraryJob(job)" in html
    assert "resumeJob(phase1StorageKey, scanCard, phase1RetryButton, scanResult, renderScanJob);" in html
    assert 'fetch("/scan/retry-errors/async"' in html
    assert "toggleSyncAuto(phase1ScheduleButton)" in html
    assert "sourceRootsField.value = rememberedSourceRoots;" in html
    assert 'id="source-picker-open"' in html
    assert 'fetch(`/source-roots/browse?' in html


def test_dashboard_people_manager_keeps_visible_alias_input(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        session.add(Person(display_name="윤겸", aliases_json=["윤겸이", "겸이"]))
        session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    html = response.text
    assert 'class="person-alias-input" name="aliases" value="윤겸이, 겸이"' in html
    assert 'placeholder="애칭 입력, 쉼표로 구분"' in html
    assert "function getAliasesFromForm(form)" in html
    assert 'input.value.split(",").map' in html


def test_merged_person_is_hidden_from_people_endpoints(client: TestClient, source_root: Path) -> None:
    with client.app.state.database.session_factory() as session:
        media = MediaFile(
            file_id="merged-person-file-1",
            current_path=str(source_root / "merged-person.jpg"),
            filename="merged-person.jpg",
            source_root=str(source_root),
            relative_path="merged-person.jpg",
            media_kind="image",
            status="active",
            size_bytes=123,
            mtime_ns=123,
            partial_hash="abc",
        )
        target = Person(display_name="윤겸", aliases_json=["겸이후보"])
        session.add_all([media, target])
        session.flush()
        hidden = Person(display_name="겸이후보", aliases_json=[], merged_into_id=target.id)
        session.add(hidden)
        session.flush()
        session.add(
            Face(
                file_id=media.file_id,
                person_id=target.id,
                bbox={"x": 0, "y": 0, "width": 10, "height": 10},
                merged_from_person_id=hidden.id,
            )
        )
        session.commit()
        target_id, hidden_id = int(target.id), int(hidden.id)

    people = client.get("/people").json()
    ids = {person["id"] for person in people}
    assert target_id in ids
    assert hidden_id not in ids
    assert client.get(f"/people/{hidden_id}").status_code == 404
    assert client.get(f"/people/{hidden_id}/preview").status_code == 404
    assert client.get(f"/people/{hidden_id}/media").status_code == 404
    assert (
        client.post(
            "/people/merge",
            json={"target_person_id": hidden_id, "source_person_ids": [target_id]},
        ).status_code
        == 404
    )

    manage = client.get("/people/manage").text
    assert "합쳐짐" in manage
    assert f"unmergePerson({target_id},{hidden_id})" in manage


def test_preferred_input_source_roots_ignores_mount_exists_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.api import status as status_api

    def raise_blocking(self: Path) -> bool:
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(status_api, "_is_docker_runtime", lambda: True)
    monkeypatch.setattr(Path, "exists", raise_blocking)
    settings = SimpleNamespace(source_root_host=None, source_root_mount=None)

    result = status_api._preferred_input_source_roots(
        settings,
        configured=["/photos"],
        known=["/Volumes/homes/dejeong/Photos"],
    )

    assert result == ["/photos"]


def test_dashboard_script_is_valid_javascript(client: TestClient, tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    response = client.get("/dashboard")
    assert response.status_code == 200
    match = re.search(r"<script>(?P<script>.*)</script>", response.text, re.S)
    assert match is not None
    script_path = tmp_path / "dashboard.js"
    script_path.write_text(match.group("script"))

    result = subprocess.run(
        [node, "--check", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_source_root_browser_lists_configured_roots(client: TestClient, source_root: Path) -> None:
    response = client.get("/source-roots/browse")

    assert response.status_code == 200
    payload = response.json()
    paths = {entry["path"] for entry in payload["entries"]}
    assert str(source_root.resolve()) in paths

    child = source_root / "nested"
    child.mkdir()
    listing = client.get("/source-roots/browse", params={"path": str(source_root)})

    assert listing.status_code == 200
    listing_payload = listing.json()
    assert listing_payload["path"] == str(source_root.resolve())
    assert any(entry["path"] == str(child.resolve()) for entry in listing_payload["entries"])


def test_source_root_browser_rejects_paths_outside_allowed_roots(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "outside-source-browser"
    outside.mkdir()

    response = client.get("/source-roots/browse", params={"path": str(outside)})

    assert response.status_code == 403


def test_dashboard_detail_renderer_escapes_server_supplied_labels(client: TestClient) -> None:
    html = client.get("/dashboard").text

    assert "${escapeHtml(item.section)}" in html
    assert "${escapeHtml(item.sublabel)}" in html
    assert "${escapeHtml(item.label)}" in html
    assert "${escapeHtml(error.message)}" in html


def test_async_semantic_job_returns_conflict_when_catalog_is_locked(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_locked(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("INSERT INTO processing_jobs ...", {}, RuntimeError("database is locked"))

    monkeypatch.setattr(client.app.state.pipeline, "submit_semantic_maintenance_job", raise_locked)

    response = client.post("/scan/semantic-maintenance/async", params={"batch_size": 10})

    assert response.status_code == 409
    assert "Another library job is still writing to the catalog" in response.json()["detail"]


def test_phase2_async_is_blocked_while_phase1_job_is_active(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        session.add(
            ProcessingJob(
                job_kind="scan",
                status="running",
                payload_json={"trigger": "test"},
                attempts=1,
            )
        )
        session.commit()

    response = client.post("/scan/semantic-maintenance/async", params={"batch_size": 10})

    assert response.status_code == 409
    assert "Phase 1 scan is already active" in response.json()["detail"]


def test_phase1_async_is_blocked_while_phase2_job_is_active(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        session.add(
            ProcessingJob(
                job_kind="semantic_maintenance",
                status="running",
                payload_json={"trigger": "test"},
                attempts=1,
            )
        )
        session.commit()

    response = client.post("/scan/async")

    assert response.status_code == 409
    assert "Phase 2 semantic work is already active" in response.json()["detail"]


def test_scan_async_returns_conflict_when_submit_lock_is_held(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """스케줄러의 run_now 스캔이 submit 락을 스캔 내내 쥐고 있는 동안
    API 제출은 무기한 대기(이벤트 루프 동결) 대신 409로 즉시 응답해야 한다."""
    import app.services.processing.pipeline as pipeline_module

    pipeline = client.app.state.pipeline
    monkeypatch.setattr(pipeline_module, "LIBRARY_SUBMIT_LOCK_TIMEOUT_SECONDS", 0.05)
    assert pipeline._library_submit_lock.acquire(blocking=False)
    try:
        response = client.post("/scan/async")
    finally:
        pipeline._library_submit_lock.release()

    assert response.status_code == 409
    assert "Phase 1 scan is already active" in response.json()["detail"]


def test_startup_recovers_interrupted_library_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    data_root = tmp_path / "data"
    derived_root = tmp_path / "derived"
    database_path = data_root / "photome.sqlite3"

    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(data_root))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(derived_root))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("TROVE_STABILITY_WINDOW_SECONDS", "1")
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TROVE_FACE_ANALYSIS_ENABLED", "0")
    monkeypatch.setenv("TROVE_CLIP_ENABLED", "0")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    settings = load_settings()
    database = build_database_state(settings)
    with database.session_factory() as session:
        job = ProcessingJob(
            job_kind="scan",
            status="running",
            payload_json={"source_roots": [str(source_root)]},
            attempts=1,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    app = create_app(settings)
    with TestClient(app) as test_client:
        status_payload = test_client.get("/status").json()
        assert status_payload["jobs"]["active_library_job"] is None
        with test_client.app.state.database.session_factory() as session:
            recovered = session.get(ProcessingJob, job_id)
            assert recovered is not None
            assert recovered.status == "canceled"
            assert recovered.error_stage == "interrupted"
            assert recovered.result_json["progress"]["resume_supported"] is True


def test_sync_auto_toggle_updates_runtime_config(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    scheduler = client.app.state.scheduler
    scheduler.stop()
    # 테스트 fixture는 env 킬스위치를 꺼두므로 토글 효과를 보려면 켠다.
    monkeypatch.setattr(scheduler, "_settings", replace(scheduler._settings, sync_scheduler_enabled=True))

    off = client.post("/scheduler/sync-auto", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["scheduler"]["sync_auto_enabled"] is False
    assert off.json()["scheduler"]["enabled"] is False
    assert off.json()["scheduler"]["next_sync_run_at"] is None

    on = client.post("/scheduler/sync-auto", json={"enabled": True})
    assert on.status_code == 200
    assert on.json()["scheduler"]["sync_auto_enabled"] is True
    assert on.json()["scheduler"]["enabled"] is True
    assert on.json()["scheduler"]["next_sync_run_at"] is not None

    bad = client.post("/scheduler/sync-auto", json={"enabled": "yes"})
    assert bad.status_code == 422

    with client.app.state.database.session_factory() as session:
        runtime_config = session.get(SchedulerRuntimeConfig, 1)
        assert runtime_config is not None
        assert bool(runtime_config.sync_enabled) is True


def test_semantic_maintenance_async_defaults_to_manual_batch_size(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.processing.pipeline import PipelineSummary

    captured: dict[str, int | bool | str] = {}

    def fake_submit(*, batch_size: int, run_now: bool, trigger: str) -> PipelineSummary:
        captured.update(batch_size=batch_size, run_now=run_now, trigger=trigger)
        return PipelineSummary(
            job_id="semantic-default-batch",
            job_kind="semantic_maintenance",
            status="queued",
            payload={"batch_size": batch_size, "trigger": trigger},
            result=None,
        )

    monkeypatch.setattr(client.app.state.pipeline, "submit_semantic_maintenance_job", fake_submit)
    monkeypatch.setattr(client.app.state.pipeline, "run_semantic_job", lambda job_id: None)

    response = client.post("/scan/semantic-maintenance/async")

    assert response.status_code == 202
    assert captured == {"batch_size": 1000, "run_now": False, "trigger": "api-async"}


def test_scheduler_tick_submits_unified_sync_only_when_work_pending(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """통합 스케줄: 주기가 되면 할 일(파일 변화/AI 백로그)이 있을 때만
    스캔+분석 통합 잡 하나를 제출한다. 별도 유휴 이미지 AI 경로는 없다."""
    from dataclasses import replace

    scheduler = client.app.state.scheduler
    scheduler.stop()
    monkeypatch.setattr(scheduler, "_settings", replace(scheduler._settings, sync_scheduler_enabled=True))
    now = datetime.utcnow()
    submitted: list[dict[str, object]] = []

    def fake_submit(**kwargs):
        submitted.append(kwargs)

    monkeypatch.setattr(client.app.state.pipeline, "submit_scan_job", fake_submit)

    # 할 일 있음 → 통합 잡 제출
    monkeypatch.setattr(client.app.state.pipeline, "has_pending_sync_work", lambda: True)
    scheduler.tick(now)
    assert len(submitted) == 1
    assert submitted[0]["full_scan"] is True
    assert submitted[0]["run_now"] is True
    assert submitted[0]["trigger"] == "scheduler-sync"
    assert scheduler.snapshot(now).last_sync_run_at is not None

    # 주기가 안 됐으면 프로브조차 안 돈다
    monkeypatch.setattr(
        client.app.state.pipeline,
        "has_pending_sync_work",
        lambda: pytest.fail("주기 전에 프로브가 호출됨"),
    )
    scheduler.tick(now + timedelta(seconds=1))
    assert len(submitted) == 1

    # 주기는 됐지만 할 일 없음 → 제출 없이 틱만 소비
    later = now + timedelta(seconds=601)
    monkeypatch.setattr(client.app.state.pipeline, "has_pending_sync_work", lambda: False)
    scheduler.tick(later)
    assert len(submitted) == 1
    last_run = scheduler.snapshot(later).last_sync_run_at
    assert last_run is not None and last_run >= later


def test_run_scan_job_persists_running_state_before_long_work(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pipeline = client.app.state.pipeline
    database = client.app.state.database
    source_root = tmp_path / "source"
    source_root.mkdir(exist_ok=True)
    image_path = source_root / "sample.jpg"
    image_path.write_bytes(b"fake")

    summary = pipeline.submit_scan_job(
        full_scan=True,
        run_now=False,
        trigger="test",
        source_roots=(source_root,),
    )

    def fake_run(self, session, progress_callback=None):  # type: ignore[no-untyped-def]
        with database.session_factory() as verify_session:
            job = verify_session.get(ProcessingJob, summary.job_id)
            assert job is not None
            assert job.status == "running"
            assert job.started_at is not None
            assert (job.result_json or {}).get("progress", {}).get("stage") == "scanning"
        return IncrementalScanSummary(scanned=1, created=0, updated=0, moved=0, missing=0, failed=0)

    monkeypatch.setattr("app.services.processing.pipeline.IncrementalScanService.run", fake_run)
    monkeypatch.setattr(pipeline, "_process_pending_media", lambda *args, **kwargs: {"pending": 0, "succeeded": 0, "failed": 0})
    monkeypatch.setattr(pipeline, "_run_scan_semantic_followup", lambda *args, **kwargs: {"pending": 0, "succeeded": 0, "failed": 0})

    result = pipeline.run_scan_job(summary.job_id)

    assert result.status == "succeeded"


def test_run_scan_job_persists_scan_count_progress_for_dashboard(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pipeline = client.app.state.pipeline
    database = client.app.state.database
    source_root = tmp_path / "source"
    source_root.mkdir(exist_ok=True)
    image_path = source_root / "sample.jpg"
    image_path.write_bytes(b"fake")

    summary = pipeline.submit_scan_job(
        full_scan=True,
        run_now=False,
        trigger="test-progress",
        source_roots=(source_root,),
    )

    def fake_run(self, session, progress_callback=None):  # type: ignore[no-untyped-def]
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "discovering_files",
                "message": f"경로를 읽고 있습니다: {source_root}",
                "source_root": str(source_root),
                "source_root_index": 1,
                "source_root_total": 1,
                "files_found": 0,
            }
        )
        progress_callback(
            {
                "stage": "processing_scan_records",
                "message": "3개의 파일을 찾았습니다. 사진 정보를 갱신 중입니다 (2/3).",
                "files_found": 3,
                "scan": {"current": 2, "total": 3, "succeeded": 2, "failed": 0},
                "current_path": str(image_path),
            }
        )
        with database.session_factory() as verify_session:
            job = verify_session.get(ProcessingJob, summary.job_id)
            assert job is not None
            progress = (job.result_json or {}).get("progress", {})
            assert progress["stage"] == "processing_scan_records"
            assert progress["files_found"] == 3
            assert progress["scan"] == {"current": 2, "total": 3, "succeeded": 2, "failed": 0}
            assert progress["source_roots"] == [str(source_root)]
            assert progress["current_path"] == str(image_path)
        return IncrementalScanSummary(scanned=3, created=0, updated=0, moved=0, missing=0, failed=0)

    monkeypatch.setattr("app.services.processing.pipeline.IncrementalScanService.run", fake_run)
    monkeypatch.setattr(pipeline, "_process_pending_media", lambda *args, **kwargs: {"pending": 0, "succeeded": 0, "failed": 0})
    monkeypatch.setattr(pipeline, "_run_scan_semantic_followup", lambda *args, **kwargs: {"pending": 0, "succeeded": 0, "failed": 0})

    result = pipeline.run_scan_job(summary.job_id)

    assert result.status == "succeeded"


def test_phase1_scan_processes_new_and_error_media_together(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = client.app.state.pipeline
    captured: dict[str, tuple[str, ...] | None] = {"statuses": None}

    def fake_run(self, session, progress_callback=None):  # type: ignore[no-untyped-def]
        return IncrementalScanSummary(scanned=0, created=0, updated=0, moved=0, missing=0, failed=0)

    def fake_process_pending_media(self, session, *, trigger_job_id, parent_job=None, statuses=("metadata_done",)):  # type: ignore[no-untyped-def]
        captured["statuses"] = statuses
        return {"pending": 0, "succeeded": 0, "failed": 0}

    monkeypatch.setattr("app.services.processing.pipeline.IncrementalScanService.run", fake_run)
    monkeypatch.setattr(type(pipeline), "_process_pending_media", fake_process_pending_media)
    monkeypatch.setattr(type(pipeline), "_run_scan_semantic_followup", lambda *args, **kwargs: {"pending": 0, "succeeded": 0, "failed": 0})

    summary = pipeline.submit_scan_job(full_scan=True, run_now=True, trigger="test-phase1")

    assert summary.status == "succeeded"
    assert captured["statuses"] == ("metadata_done", "error", "missing")


def test_scan_job_runs_semantic_followup_by_default(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = client.app.state.pipeline
    calls: list[int] = []

    def fake_run(self, session, progress_callback=None):  # type: ignore[no-untyped-def]
        return IncrementalScanSummary(scanned=0, created=0, updated=0, moved=0, missing=0, failed=0)

    monkeypatch.setattr("app.services.processing.pipeline.IncrementalScanService.run", fake_run)
    monkeypatch.setattr(type(pipeline), "_process_pending_media", lambda *args, **kwargs: {"pending": 0, "succeeded": 0, "failed": 0})

    def fake_semantic_followup(self, session, job, *, batch_size):  # type: ignore[no-untyped-def]
        calls.append(batch_size)
        return {"pending": 3, "succeeded": 3, "failed": 0}

    monkeypatch.setattr(type(pipeline), "_run_scan_semantic_followup", fake_semantic_followup)

    summary = pipeline.submit_scan_job(full_scan=True, run_now=True, trigger="test-phase1-followup")

    assert summary.status == "succeeded"
    assert calls == [500]


def test_run_semantic_job_persists_running_state_before_long_work(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = client.app.state.pipeline
    database = client.app.state.database

    summary = pipeline.submit_semantic_maintenance_job(
        batch_size=10,
        run_now=False,
        trigger="test",
    )

    def fake_maintenance(*, batch_size: int, progress_callback=None):  # type: ignore[no-untyped-def]
        with database.session_factory() as verify_session:
            job = verify_session.get(ProcessingJob, summary.job_id)
            assert job is not None
            assert job.status == "running"
            assert job.started_at is not None
            assert (job.result_json or {}).get("progress", {}).get("stage") == "collecting"
        if progress_callback is not None:
            progress_callback({"mode": "maintenance", "pending": 0, "current": 0, "succeeded": 0, "failed": 0})
        return {"skipped": False, "pending": 0, "succeeded": 0, "failed": 0, "has_more": False}

    monkeypatch.setattr(pipeline, "run_semantic_maintenance", fake_maintenance)

    result = pipeline.run_semantic_job(summary.job_id)

    assert result.status == "succeeded"


def test_async_semantic_job_runs_chunks_until_exhausted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = client.app.state.pipeline
    summary = pipeline.submit_semantic_maintenance_job(
        batch_size=10,
        run_now=False,
        trigger="test",
    )
    chunks = [
        {
            "skipped": False,
            "pending": 10,
            "succeeded": 10,
            "failed": 0,
            "has_more": True,
            "embeddings_created": 10,
            "auto_tag_files": 4,
            "auto_tag_values": 20,
            "search_documents_updated": 10,
        },
        {
            "skipped": False,
            "pending": 3,
            "succeeded": 3,
            "failed": 0,
            "has_more": False,
            "embeddings_created": 3,
            "auto_tag_files": 2,
            "auto_tag_values": 8,
            "search_documents_updated": 3,
        },
    ]
    calls = {"count": 0}

    def fake_maintenance(*, batch_size: int, progress_callback=None):  # type: ignore[no-untyped-def]
        result = chunks[calls["count"]]
        calls["count"] += 1
        if progress_callback is not None:
            progress_callback({
                "mode": "maintenance",
                "pending": result["pending"],
                "current": result["pending"],
                "succeeded": result["succeeded"],
                "failed": result["failed"],
                "batch_size": batch_size,
                "embeddings_created": result["embeddings_created"],
                "auto_tag_files": result["auto_tag_files"],
                "auto_tag_values": result["auto_tag_values"],
                "search_documents_updated": result["search_documents_updated"],
            })
        return result

    monkeypatch.setattr(pipeline, "run_semantic_maintenance", fake_maintenance)

    result = pipeline.run_semantic_job(summary.job_id)

    assert calls["count"] == 2
    assert result.status == "succeeded"
    assert result.result is not None
    assert result.result["full_run"] is True
    assert result.result["chunks"] == 2
    assert result.result["succeeded"] == 13
    assert result.result["embeddings_created"] == 13
    assert result.result["auto_tag_values"] == 28
    assert result.result["search_documents_updated"] == 13


def test_scan_rejects_missing_source_root(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/scan", params={"source_roots": str(tmp_path / "missing")})

    assert response.status_code == 400
    assert "Source root does not exist" in response.json()["detail"]


def test_scan_missing_source_root_explains_docker_mount(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / ".dockerenv"
    marker.touch()
    import app.api.scan as scan_api

    original_exists = scan_api.os.path.exists

    def fake_exists(path: str) -> bool:
        if path == "/.dockerenv":
            return True
        return original_exists(path)

    monkeypatch.setattr(scan_api.os.path, "exists", fake_exists)

    response = client.post("/scan", params={"source_roots": "/Volumes/__nonexistent_test_path_abc123__"})

    assert response.status_code == 400
    assert "Source root does not exist" in response.json()["detail"]
    assert "Finder/host NAS paths" in response.json()["detail"]


def test_scan_missing_mapped_source_root_explains_auto_mapping(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.scan as scan_api

    marker = tmp_path / ".dockerenv"
    marker.touch()
    original_exists = scan_api.os.path.exists

    def fake_exists(path: str) -> bool:
        if path == "/.dockerenv":
            return True
        return original_exists(path)

    monkeypatch.setattr(scan_api.os.path, "exists", fake_exists)
    monkeypatch.setenv("TROVE_SOURCE_ROOT_HOST", "/Volumes/homes/dejeong/Photos")
    monkeypatch.setenv("TROVE_SOURCE_ROOT_MOUNT", "/photos")

    app = create_app(load_settings())
    with TestClient(app) as mapped_client:
        response = mapped_client.post("/scan", params={"source_roots": "/Volumes/homes/dejeong/Photos/missing"})

    assert response.status_code == 400
    assert "Source root does not exist" in response.json()["detail"]
    assert "auto-map Finder/NAS paths" in response.json()["detail"]


def scan_twice(client: TestClient) -> tuple[dict, dict]:
    first = client.post("/scan").json()
    time.sleep(SCAN_DELAY_SECONDS)
    second = client.post("/scan").json()
    return first, second


def create_image(path: Path) -> None:
    image = Image.new("RGB", (80, 60), color=(40, 80, 120))
    image.save(path, format="JPEG")


def test_search_suggest_ranks_prefix_tags_and_recent_queries(
    client: TestClient, source_root: Path
) -> None:
    create_image(source_root / "suggest-a.jpg")
    create_image(source_root / "suggest-b.jpg")
    scan_twice(client)

    with client.app.state.database.session_factory() as session:
        files = session.scalars(select(MediaFile)).all()
        assert len(files) >= 2
        fid, fid2 = files[0].file_id, files[1].file_id
        session.add_all([
            Tag(file_id=fid, tag_type="place", tag_value="바다"),
            Tag(file_id=fid2, tag_type="place", tag_value="바다"),       # freq 2
            Tag(file_id=fid, tag_type="object", tag_value="바닐라라떼"),  # freq 1, prefix
            Tag(file_id=fid, tag_type="object", tag_value="강아지바"),     # substring only
            Tag(file_id=fid, tag_type="person", tag_value="person-123456"),  # internal id, hidden
        ])
        session.add(SearchEvent(query="바람 부는 날", effective_mode="hybrid", intent="visual", result_count=5))
        session.add(SearchEvent(query="바보", effective_mode="hybrid", intent="visual", result_count=0))
        session.commit()

    data = client.get("/search/suggest", params={"q": "바"}).json()
    suggestions = data["suggestions"]
    values = [s["value"] for s in suggestions]

    assert "person-123456" not in values                         # internal id hidden
    assert values.index("바다") < values.index("강아지바")          # prefix beats substring
    assert values.index("바닐라라떼") < values.index("강아지바")
    assert values.index("바다") < values.index("바닐라라떼")        # within prefix group: freq desc
    assert "바람 부는 날" in values                                # recent successful query
    assert next(s for s in suggestions if s["value"] == "바람 부는 날")["kind"] == "recent"
    assert "바보" not in values                                    # zero-result query excluded
    sea = next(s for s in suggestions if s["value"] == "바다")
    assert sea["kind"] == "tag" and sea["count"] == 2


def test_search_suggest_blank_query_returns_empty(client: TestClient) -> None:
    response = client.get("/search/suggest", params={"q": "  "})
    assert response.status_code == 200
    assert response.json()["suggestions"] == []


def test_load_query_feedback_scopes_promote_and_returns_corrections(
    client: TestClient, source_root: Path
) -> None:
    from app.services.search.backend import SqlAlchemyHybridSearchBackend

    create_image(source_root / "fb-a.jpg")
    create_image(source_root / "fb-b.jpg")
    create_image(source_root / "fb-c.jpg")
    scan_twice(client)

    settings = client.app.state.settings
    with client.app.state.database.session_factory() as session:
        files = session.scalars(select(MediaFile)).all()
        assert len(files) >= 3
        a, b, c = files[0].file_id, files[1].file_id, files[2].file_id
        session.add_all([
            SearchFeedback(file_id=a, action="promote", query_hint="바다"),
            SearchFeedback(file_id=b, action="promote", query_hint="강아지"),
            SearchFeedback(file_id=c, action="correct_tag", query_hint="", tag_correction="노을"),
        ])
        session.commit()

        backend = SqlAlchemyHybridSearchBackend(
            session,
            embeddings_root=settings.embeddings_root,
            clip_enabled=settings.semantic_clip_enabled,
            log_events=False,
        )
        pinned, corrections = backend.load_query_feedback("바다 노을 사진")

    assert a in pinned              # hint "바다" ⊆ query tokens → pinned
    assert b not in pinned          # hint "강아지" absent from query → not pinned
    assert corrections.get(c) == "노을"  # correct_tag surfaced for the reranker


def test_scan_cache_excludes_dirs_with_unstable_files(
    client: TestClient,
    source_root: Path,
) -> None:
    """안정화 대기 파일이 있는 폴더는 dirty 센티널(-1)로 캐시에 남는다.

    실제 mtime으로 남기면 다음 스캔이 폴더를 건너뛰어 그 파일이 영영
    ingest되지 않고, 키를 아예 빼면 walk 시딩에서 빠져 조상 mtime이 안
    변한 중첩 폴더가 영영 재방문되지 않는다.
    """
    import json

    create_image(source_root / "fresh-unstable.jpg")
    client.post("/scan")  # 생성 직후라 안정화 창(1초)을 못 넘긴 상태

    cache_path = Path(client.app.state.settings.data_root) / "scan_cache.json"
    assert cache_path.is_file()
    assert json.loads(cache_path.read_text(encoding="utf-8")).get(str(source_root)) == -1

    time.sleep(SCAN_DELAY_SECONDS)
    client.post("/scan")

    assert json.loads(cache_path.read_text(encoding="utf-8")).get(str(source_root), -1) > 0
    items = client.get("/media", params={"q": "unstable"}).json()["items"]
    assert len(items) == 1


def test_batch_load_supplements_handles_huge_id_lists(client: TestClient) -> None:
    """갤러리 NL 검색은 후보 한도가 사실상 무제한이라 file_id가 수만 개까지
    커진다 — IN 절을 통째로 바인딩하면 SQLite 'too many SQL variables'로
    500이 났다('이한이랑 물에서 찍은 사진' 실사고). 청크 분할 회귀 가드."""
    from app.services.search.backend import SqlAlchemyHybridSearchBackend

    settings = client.app.state.settings
    with client.app.state.database.session_factory() as session:
        backend = SqlAlchemyHybridSearchBackend(
            session,
            embeddings_root=settings.embeddings_root,
            clip_enabled=False,
            log_events=False,
        )
        huge_ids = [f"{index:064d}" for index in range(40000)]
        tags_by_file, face_counts, person_counts, ocr_by_file, analysis_by_file = (
            backend._batch_load_supplements(huge_ids)
        )

    assert len(tags_by_file) == 40000
    assert face_counts == {} and person_counts == {}
    assert ocr_by_file == {} and analysis_by_file == {}
