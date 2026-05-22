"""Derived asset registry model."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class DerivedAsset(Base):
    __tablename__ = "derived_assets"
    __table_args__ = (
        UniqueConstraint("file_id", "asset_kind", "asset_version", "derived_path", name="uq_derived_asset_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    asset_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    derived_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    media_file: Mapped["MediaFile"] = relationship(back_populates="assets")
