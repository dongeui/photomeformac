from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.contracts import MediaTagInput
from app.models.base import Base
from app.models.media import MediaFile
from app.models.semantic import MediaAutoTagState
from app.services.analysis import auto_tags
from app.services.analysis.filename_lexicon import load_filename_tag_rules
from app.services.semantic import SemanticCatalog


def test_auto_tags_from_signals_are_conservative_and_deduped() -> None:
    tags = auto_tags.tags_from_signals(
        {
            "is_screenshot_like": True,
            "is_document_like": True,
            "is_text_heavy": True,
        },
        "카드 승인 합계 12000",
    )

    assert [(tag.tag_type, tag.tag_value) for tag in tags] == [
        ("auto_screen", "screenshot"),
        ("auto_screen", "document"),
        ("auto_screen", "text"),
        ("auto_screen", "receipt"),
    ]


def test_clip_concept_tags_expand_natural_language_aliases(monkeypatch) -> None:
    basis = {
        "beach": np.array([1.0, 0.0, 0.0], dtype="float32"),
        "sea": np.array([0.98, 0.02, 0.0], dtype="float32"),
        "water": np.array([0.9, 0.1, 0.0], dtype="float32"),
    }

    def fake_concept_vector(tag: str) -> np.ndarray:
        return basis.get(tag, np.array([0.0, 1.0, 0.0], dtype="float32"))

    monkeypatch.setattr(auto_tags, "_concept_vector", fake_concept_vector)

    tags = auto_tags.tags_from_embedding_vector(np.array([1.0, 0.0, 0.0], dtype="float32"))
    values = [tag.tag_value for tag in tags]

    assert "beach" in values
    assert "sea" in values
    assert "ocean" in values
    assert "바다" in values


def test_filename_tag_rules_are_packaged_yaml() -> None:
    rules = load_filename_tag_rules()

    assert rules
    tags = auto_tags.tags_from_filename("family_beach_trip_2024.jpg")
    assert [(tag.tag_type, tag.tag_value) for tag in tags] == [
        ("auto_scene", "beach"),
        ("auto_scene", "travel"),
        ("auto_person", "group"),
    ]


def test_clear_auto_tag_caches_clears_concept_vector_cache(monkeypatch) -> None:
    monkeypatch.setattr(auto_tags.clip_embedding, "ensure_models", lambda: None)
    monkeypatch.setattr(
        auto_tags.clip_embedding,
        "encode_text",
        lambda _prompt: np.array([1.0, 0.0, 0.0], dtype="float32").tobytes(),
    )
    monkeypatch.setattr(
        auto_tags.clip_embedding,
        "embedding_from_bytes",
        lambda _payload: np.array([1.0, 0.0, 0.0], dtype="float32"),
    )

    auto_tags._concept_vectors()
    assert auto_tags._concept_vectors.cache_info().currsize == 1

    auto_tags.clear_auto_tag_caches()
    assert auto_tags._concept_vectors.cache_info().currsize == 0


def test_auto_tag_state_records_version(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'auto-tags.sqlite3'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        now = datetime.utcnow()
        session.add(
            MediaFile(
                file_id="sample-file-id",
                current_path="/tmp/sample.jpg",
                filename="sample.jpg",
                source_root="/tmp",
                relative_path="sample.jpg",
                media_kind="image",
                status="thumb_done",
                size_bytes=1,
                mtime_ns=1,
                partial_hash="hash",
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        state = SemanticCatalog(session).upsert_auto_tag_state(
            "sample-file-id",
            tags=[MediaTagInput(tag_type="auto_screen", tag_value="screenshot")],
            version="auto-v2",
        )
        session.commit()

        reloaded = session.get(MediaAutoTagState, "sample-file-id")
        assert reloaded is not None
        assert reloaded.version == "auto-v2"
        assert reloaded.tags_json == [{"type": "auto_screen", "value": "screenshot"}]
        assert state.file_id == "sample-file-id"
