"""Database bootstrap and runtime state."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import AppSettings
from app.db.session import create_engine_for_settings, create_session_factory
from app.models.base import Base
from app.models import annotation, asset, face, job, media, observation, person, runtime, semantic, tag  # noqa: F401  ensure models are registered


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseState:
    settings: AppSettings
    engine: Engine
    session_factory: sessionmaker[Session]

    @property
    def configured(self) -> bool:
        return True

    @property
    def database_url(self) -> str:
        return self.settings.database_url


def _ensure_runtime_directories(settings: AppSettings) -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.derived_root.mkdir(parents=True, exist_ok=True)
    settings.thumbnail_root.mkdir(parents=True, exist_ok=True)
    settings.preview_root.mkdir(parents=True, exist_ok=True)
    settings.keyframe_root.mkdir(parents=True, exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)


def build_database_state(settings: AppSettings) -> DatabaseState:
    _ensure_runtime_directories(settings)
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    _ensure_search_document_fts(engine)
    _ensure_tag_indexes(engine)
    _ensure_people_aliases_column(engine)
    _ensure_face_version_column(engine)
    _ensure_geocoding_aliases_column(engine)
    _migrate_auto_tag_types(engine)
    session_factory = create_session_factory(engine)
    logger.info("database bootstrapped", extra={"database_url": settings.database_url})
    return DatabaseState(settings=settings, engine=engine, session_factory=session_factory)


def _ensure_tag_indexes(engine: Engine) -> None:
    """Idempotently create compound tag index for existing databases.

    create_all() creates the index for new DBs, but skips it on existing ones.
    """
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_tags_type_value ON tags (tag_type, tag_value)"
                )
            )
    except Exception as exc:
        logger.warning("tag compound index creation failed", extra={"error": str(exc)})


def _ensure_people_aliases_column(engine: Engine) -> None:
    """Add user-managed person aliases to existing SQLite catalogs."""
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(people)")).all()}
            if "aliases_json" not in columns:
                conn.execute(text("ALTER TABLE people ADD COLUMN aliases_json JSON NOT NULL DEFAULT '[]'"))
    except Exception as exc:
        logger.warning("people aliases migration failed", extra={"error": str(exc)})


def _ensure_face_version_column(engine: Engine) -> None:
    """Add face_version tracking to existing SQLite catalogs."""
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(media_files)")).all()}
            if "face_version" not in columns:
                conn.execute(text("ALTER TABLE media_files ADD COLUMN face_version VARCHAR(16)"))
    except Exception as exc:
        logger.warning("face_version column migration failed", extra={"error": str(exc)})


def _ensure_geocoding_aliases_column(engine: Engine) -> None:
    """Add multilingual place aliases to existing SQLite geocoding cache rows."""
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.begin() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(geocoding_cache)")).all()}
            if "aliases_json" not in columns:
                conn.execute(text("ALTER TABLE geocoding_cache ADD COLUMN aliases_json JSON NOT NULL DEFAULT '[]'"))
    except Exception as exc:
        logger.warning("geocoding aliases migration failed", extra={"error": str(exc)})


def _migrate_auto_tag_types(engine: Engine) -> None:
    """One-time migration: reclassify tag_type='auto' rows into typed subtypes.

    Safe to run on every startup — rows already reclassified are left untouched.
    """
    if engine.dialect.name != "sqlite":
        return
    _TYPE_MAP: dict[str, str] = {
        # screen
        "kakaotalk": "auto_screen", "screenshot": "auto_screen", "document": "auto_screen",
        "receipt": "auto_screen", "text": "auto_screen", "screen": "auto_screen",
        # person
        "person": "auto_person", "baby": "auto_person", "woman": "auto_person",
        "man": "auto_person", "child": "auto_person", "group": "auto_person",
        "infant": "auto_person", "newborn": "auto_person", "toddler": "auto_person",
        "female": "auto_person", "girl": "auto_person", "male": "auto_person",
        "boy": "auto_person", "kid": "auto_person",
        # event
        "celebration": "auto_event", "wedding": "auto_event", "birthday": "auto_event",
        # scene
        "outdoor": "auto_scene", "beach": "auto_scene", "sea": "auto_scene",
        "water": "auto_scene", "ocean": "auto_scene", "coast": "auto_scene",
        "mountain": "auto_scene", "nature": "auto_scene", "sky": "auto_scene",
        "travel": "auto_scene", "night": "auto_scene", "sunset": "auto_scene",
        "spring": "auto_scene", "summer": "auto_scene", "autumn": "auto_scene",
        "winter": "auto_scene",
        # object
        "food": "auto_object", "cake": "auto_object", "coffee": "auto_object",
        "vehicle": "auto_object", "animal": "auto_object", "meal": "auto_object",
    }
    try:
        with engine.begin() as conn:
            for tag_value, new_type in _TYPE_MAP.items():
                conn.execute(
                    text(
                        "UPDATE tags SET tag_type = :new_type "
                        "WHERE tag_type = 'auto' AND LOWER(tag_value) = :tag_value"
                    ),
                    {"new_type": new_type, "tag_value": tag_value},
                )
            # Remaining unrecognised 'auto' rows become auto_scene as a safe default
            conn.execute(
                text("UPDATE tags SET tag_type = 'auto_scene' WHERE tag_type = 'auto'")
            )
        logger.info("auto tag type migration complete")
    except Exception as exc:
        logger.warning("auto tag type migration failed", extra={"error": str(exc)})


def _ensure_search_document_fts(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        # Primary FTS — unicode61 (word-boundary, good for English)
        try:
            connection.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts
                    USING fts5(
                        file_id UNINDEXED,
                        search_text,
                        keyword_text,
                        semantic_text,
                        tokenize='unicode61'
                    )
                    """
                )
            )
        except Exception as exc:
            logger.warning("search document FTS unavailable", extra={"error": str(exc)})

        # Trigram FTS — character n-gram, better for Korean/CJK substring search.
        # Requires SQLite 3.34.0+ (2020). Falls back silently if unavailable.
        try:
            connection.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts_ko
                    USING fts5(
                        file_id UNINDEXED,
                        search_text,
                        keyword_text,
                        semantic_text,
                        tokenize='trigram'
                    )
                    """
                )
            )
        except Exception as exc:
            logger.info(
                "trigram FTS unavailable (SQLite < 3.34); Korean substring search will use n-gram fallback",
                extra={"error": str(exc)},
            )
