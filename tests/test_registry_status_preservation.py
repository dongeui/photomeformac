from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.contracts import DerivedAssetKind, FileIdentity, FileScanRecord, MediaFaceInput, MediaKind, MediaMetadata
from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401
from app.models.base import Base
from app.models.face import Face
from app.models.person import Person
from app.services.processing.registry import MediaCatalog


def test_rescanning_unchanged_processed_media_preserves_thumb_done_status() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    source_root = Path("/photos")
    first_path = source_root / "before.jpg"
    moved_path = source_root / "after.jpg"
    identity = FileIdentity(
        file_id="file-1",
        size_bytes=123,
        mtime_ns=456,
        partial_hash="abc",
        media_kind=MediaKind.IMAGE,
    )
    metadata = MediaMetadata(kind=MediaKind.IMAGE, width=64, height=48)
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        catalog = MediaCatalog(session)
        catalog.upsert_media_file(
            _record(source_root, first_path),
            identity,
            metadata,
            now=now,
        )
        catalog.register_derived_asset("file-1", DerivedAssetKind.THUMBNAIL, Path("thumb/v1/fi/file-1.jpg"))
        catalog.set_media_status("file-1", status="thumb_done", now=now)
        session.commit()

        catalog.upsert_media_file(
            _record(source_root, moved_path),
            identity,
            metadata,
            now=now,
        )
        session.commit()

        media_file = catalog.get_media("file-1")
        assert media_file is not None
        assert media_file.current_path == str(moved_path)
        assert media_file.status == "thumb_done"


def test_replacing_faces_preserves_existing_person_assignment_for_same_face() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    source_root = Path("/photos")
    path = source_root / "family.jpg"
    identity = FileIdentity(
        file_id="file-1",
        size_bytes=123,
        mtime_ns=456,
        partial_hash="abc",
        media_kind=MediaKind.IMAGE,
    )
    metadata = MediaMetadata(kind=MediaKind.IMAGE, width=64, height=48)
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        catalog = MediaCatalog(session)
        catalog.upsert_media_file(_record(source_root, path), identity, metadata, now=now)
        target = Person(display_name="엄마", aliases_json=["mom"])
        accidental = Person(display_name="person-999999", aliases_json=[])
        session.add_all([target, accidental])
        session.flush()
        session.add(
            Face(
                file_id="file-1",
                person_id=target.id,
                bbox={"x": 10, "y": 10, "width": 20, "height": 20},
                embedding_ref="old-face.json",
            )
        )
        session.commit()

        catalog.upsert_faces(
            "file-1",
            [
                MediaFaceInput(
                    person_id=accidental.id,
                    bbox={"x": 11, "y": 9, "width": 20, "height": 20},
                    embedding_ref="new-face.json",
                )
            ],
        )
        session.commit()

        refreshed_face = session.scalars(select(Face).where(Face.file_id == "file-1")).one()
        assert refreshed_face.person_id == target.id


def _record(source_root: Path, path: Path) -> FileScanRecord:
    return FileScanRecord(
        source_root=source_root,
        path=path,
        relative_path=path.relative_to(source_root),
        size_bytes=123,
        mtime_ns=456,
        media_kind=MediaKind.IMAGE,
    )
