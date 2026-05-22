"""Primary media catalog model."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Float, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MediaFile(Base):
    __tablename__ = "media_files"

    file_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    current_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source_root: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partial_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    fingerprint_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    face_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, default=None)
    width: Mapped[Optional[int]] = mapped_column(nullable=True)
    height: Mapped[Optional[int]] = mapped_column(nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    codec_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    exif_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    stable_after_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    error_count: Mapped[int] = mapped_column(nullable=False, default=0)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    assets: Mapped[list["DerivedAsset"]] = relationship(
        back_populates="media_file",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list["Tag"]] = relationship(
        back_populates="media_file",
        cascade="all, delete-orphan",
    )
    faces: Mapped[list["Face"]] = relationship(
        back_populates="media_file",
        cascade="all, delete-orphan",
    )
