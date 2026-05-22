"""Semantic enrichment models for OCR, image signals, and CLIP embeddings."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MediaOCR(Base):
    __tablename__ = "media_ocr"

    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), primary_key=True)
    text_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    engine: Mapped[str] = mapped_column(String(64), nullable=False, default="tesseract")
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="ocr-v1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    media_file = relationship("MediaFile")


class MediaOCRBlock(Base):
    __tablename__ = "media_ocr_blocks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    left: Mapped[int] = mapped_column(nullable=False, default=0)
    top: Mapped[int] = mapped_column(nullable=False, default=0)
    width: Mapped[int] = mapped_column(nullable=False, default=0)
    height: Mapped[int] = mapped_column(nullable=False, default=0)


class MediaOCRGram(Base):
    __tablename__ = "media_ocr_grams"
    __table_args__ = (UniqueConstraint("file_id", "gram", name="uq_media_ocr_gram"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    gram: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    count: Mapped[int] = mapped_column(nullable=False, default=1)


class MediaAnalysisSignal(Base):
    __tablename__ = "media_analysis_signals"

    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), primary_key=True)
    text_char_count: Mapped[int] = mapped_column(nullable=False, default=0)
    text_line_count: Mapped[int] = mapped_column(nullable=False, default=0)
    edge_density: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    brightness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_text_heavy: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_document_like: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_screenshot_like: Mapped[bool] = mapped_column(nullable=False, default=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="analysis-v1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    media_file = relationship("MediaFile")


class MediaEmbedding(Base):
    __tablename__ = "media_embeddings"
    __table_args__ = (UniqueConstraint("file_id", "model_name", "version", name="uq_media_embedding_version"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    dimensions: Mapped[int] = mapped_column(nullable=False)
    checksum: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class MediaAutoTagState(Base):
    __tablename__ = "media_auto_tag_states"

    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="thumb+clip")
    tags_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    media_file = relationship("MediaFile")


class SearchWeightProfile(Base):
    """Persisted channel weights per intent.

    Each row is keyed by (intent, reason) — the same identifiers returned by
    resolve_effective_mode(). Weights override the built-in defaults and survive
    server restarts.

    Example keys:
      intent='ocr',      reason='manual'
      intent='semantic', reason='auto-travel'
      intent='hybrid',   reason='fallback'
    """

    __tablename__ = "search_weight_profiles"
    __table_args__ = (
        UniqueConstraint("intent", "reason", name="uq_search_weight_profile"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    w_ocr: Mapped[float] = mapped_column(Float, nullable=False, default=0.35)
    w_clip: Mapped[float] = mapped_column(Float, nullable=False, default=0.36)
    w_shadow: Mapped[float] = mapped_column(Float, nullable=False, default=0.17)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class GeocodingCache(Base):
    """Cached reverse geocoding results keyed by truncated GPS coordinate.

    The key is "{lat:.3f},{lon:.3f}" (same precision used for place tags)
    so nearby photos share a single cache entry.
    """

    __tablename__ = "geocoding_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    country: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    region: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    place: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MediaCaption(Base):
    """VLM-generated caption for a media file.

    Stored separately from SearchDocument so it can be versioned and
    re-generated independently of other semantic signals.
    """

    __tablename__ = "media_captions"

    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), primary_key=True)
    short_caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    objects_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    activities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    setting: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="caption-v1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    media_file = relationship("MediaFile")


class SearchFeedback(Base):
    """User feedback signals for search results.

    action values:
      'hide'     — exclude this file from future search results globally
      'promote'  — boost this file's rank in search results
      'correct_tag' — user-supplied correction; tag_correction holds the value

    query_hint is optional: if provided the feedback is scoped to that query;
    if empty it applies globally (e.g. hide always).
    """

    __tablename__ = "search_feedback"
    __table_args__ = (
        UniqueConstraint("file_id", "action", "query_hint", name="uq_search_feedback"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)       # hide | promote | correct_tag
    query_hint: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    tag_correction: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    media_file = relationship("MediaFile")


class SearchDocument(Base):
    __tablename__ = "search_documents"

    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    keyword_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    semantic_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    people_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    places_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    signals_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    embedding_refs_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    media_file = relationship("MediaFile")


class SearchEvent(Base):
    """Implicit feedback: lightweight log of every search query and its outcome.

    Stored locally to power future auto weight tuning and zero-result analysis.
    Not a user-facing table — records are written-only and can be pruned after N days.

    fallback: None | 'date_relaxed' | 'fuzzy_corrected' — which fallback path fired
    """

    __tablename__ = "search_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    effective_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    result_count: Mapped[int] = mapped_column(nullable=False, default=0)
    fallback: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
