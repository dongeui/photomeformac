"""폴더 전환(A→B) 동작 정의 회귀 가드.

확정된 정의: 폴더를 바꾸면 기존에 동기화한 사진(썸네일·AI 결과)은 그대로
보존하고, 새 폴더의 이미지만 들여오며, 이후 동기화·분석은 지정된 폴더만
본다. 보존된 사진은 갤러리에 계속 보이되 처리 큐에서는 빠진다(archived).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.contracts import FileIdentity, FileScanRecord, MediaKind, MediaMetadata
from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.services.processing.registry import MediaCatalog

OLD_ROOT = Path("/photos/old")
NEW_ROOT = Path("/photos/new")


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _ingest(catalog: MediaCatalog, root: Path, name: str, file_id: str) -> None:
    path = root / name
    identity = FileIdentity(
        file_id=file_id,
        size_bytes=100,
        mtime_ns=1,
        partial_hash=f"hash-{file_id}",
        media_kind=MediaKind.IMAGE,
    )
    record = FileScanRecord(
        path=path,
        source_root=root,
        relative_path=Path(name),
        size_bytes=100,
        mtime_ns=1,
        media_kind=MediaKind.IMAGE,
    )
    now = datetime.now(timezone.utc)
    catalog.upsert_media_file(record, identity, MediaMetadata(kind=MediaKind.IMAGE), now=now)
    catalog.set_media_status(file_id, status="analysis_done", now=now)


def test_folder_switch_preserves_old_root_records() -> None:
    """새 폴더만 스캔해도 옛 폴더 사진은 missing 처리되지 않는다."""
    factory = _session_factory()
    with factory() as session:
        catalog = MediaCatalog(session)
        _ingest(catalog, OLD_ROOT, "a.jpg", "old-1")
        _ingest(catalog, NEW_ROOT, "b.jpg", "new-1")
        session.commit()

        # 새 폴더만 활성인 스캔: 새 폴더에서 본 파일만 seen으로 보고
        missing = catalog.mark_missing_except(
            seen_paths={str(NEW_ROOT / "b.jpg")},
            active_source_roots={str(NEW_ROOT)},
        )
        session.commit()

        assert missing == 0
        assert catalog.get_media("old-1").status == "analysis_done"
        assert catalog.get_media("new-1").status == "analysis_done"


def test_retire_missing_source_archives_inactive_root() -> None:
    """원본이 사라진 옛 루트 사진은 missing이 아니라 archived — 갤러리 노출 유지."""
    factory = _session_factory()
    now = datetime.now(timezone.utc)
    with factory() as session:
        catalog = MediaCatalog(session)
        _ingest(catalog, OLD_ROOT, "a.jpg", "old-1")
        session.commit()

        outcome = catalog.retire_missing_source(
            "old-1",
            source_root=str(OLD_ROOT),
            active_source_roots={str(NEW_ROOT)},
            now=now,
        )
        session.commit()

        assert outcome == "archived"
        status = catalog.get_media("old-1").status
        assert status == "archived"
        # 갤러리 노출 조건(missing/replaced/excluded 제외)을 통과해야 한다
        assert status not in ("missing", "replaced", "excluded")


def test_retire_missing_source_marks_active_root_missing() -> None:
    """활성 루트에서 사라진 파일은 기존대로 missing(스캔이 복원 관리)."""
    factory = _session_factory()
    with factory() as session:
        catalog = MediaCatalog(session)
        _ingest(catalog, NEW_ROOT, "b.jpg", "new-1")
        session.commit()

        outcome = catalog.retire_missing_source(
            "new-1",
            source_root=str(NEW_ROOT),
            active_source_roots={str(NEW_ROOT)},
            now=datetime.now(timezone.utc),
        )
        session.commit()

        assert outcome == "missing"
        assert catalog.get_media("new-1").status == "missing"


def test_archived_leaves_processing_queue() -> None:
    """archived는 임베딩 후보 선정(thumb_done/analysis_done)에서 빠진다."""
    factory = _session_factory()
    now = datetime.now(timezone.utc)
    with factory() as session:
        catalog = MediaCatalog(session)
        _ingest(catalog, OLD_ROOT, "a.jpg", "old-1")
        session.commit()

        assert [m.file_id for m in catalog.list_media_needing_embedding()] == ["old-1"]
        catalog.set_media_status("old-1", status="archived", now=now)
        session.commit()
        assert catalog.list_media_needing_embedding() == []


def test_rescanning_old_root_revives_archived_record() -> None:
    """옛 폴더를 다시 선택하면 archived 사진이 같은 file_id로 재편입된다."""
    factory = _session_factory()
    now = datetime.now(timezone.utc)
    with factory() as session:
        catalog = MediaCatalog(session)
        _ingest(catalog, OLD_ROOT, "a.jpg", "old-1")
        catalog.set_media_status("old-1", status="archived", now=now)
        session.commit()

        # 되살리기 재확인 대상에 archived도 포함된다
        revive_targets = list(catalog.iter_missing_media_paths({str(OLD_ROOT)}))
        assert (str(OLD_ROOT), str(OLD_ROOT / "a.jpg")) in revive_targets

        # 재스캔 upsert가 처리 파이프라인으로 되돌린다 (archived → metadata_done)
        _ingest_record = FileScanRecord(
            path=OLD_ROOT / "a.jpg",
            source_root=OLD_ROOT,
            relative_path=Path("a.jpg"),
            size_bytes=100,
            mtime_ns=1,
            media_kind=MediaKind.IMAGE,
        )
        identity = FileIdentity(
            file_id="old-1",
            size_bytes=100,
            mtime_ns=1,
            partial_hash="hash-old-1",
            media_kind=MediaKind.IMAGE,
        )
        catalog.upsert_media_file(_ingest_record, identity, MediaMetadata(kind=MediaKind.IMAGE), now=now)
        session.commit()
        assert catalog.get_media("old-1").status == "metadata_done"


def test_download_of_unavailable_original_shows_friendly_page(monkeypatch, tmp_path) -> None:
    """원본 경로가 해제된 사진의 다운로드는 500 대신 안내 페이지(410)."""
    from fastapi.testclient import TestClient

    from app.core.settings import load_settings
    from app.main import create_app

    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setenv("TROVE_SOURCE_ROOTS", str(source_root))
    monkeypatch.setenv("TROVE_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("TROVE_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("TROVE_DATABASE_PATH", str(tmp_path / "data" / "test.sqlite3"))
    monkeypatch.setenv("TROVE_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("TROVE_LOG_LEVEL", "ERROR")

    app = create_app(load_settings())
    with TestClient(app) as client:
        with app.state.database.session_factory() as session:
            catalog = MediaCatalog(session)
            _ingest(catalog, OLD_ROOT, "gone.jpg", "old-1")
            session.commit()

        response = client.get("/media/old-1/download")
        assert response.status_code == 410
        assert "원본을 열 수 없습니다" in response.text
        assert "썸네일과 검색은 계속" in response.text
