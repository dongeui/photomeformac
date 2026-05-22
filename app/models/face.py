"""Face model for future face-aware filters."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Face(Base):
    __tablename__ = "faces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), nullable=True)
    bbox: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    embedding_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    media_file: Mapped["MediaFile"] = relationship(back_populates="faces")
    person: Mapped[Optional["Person"]] = relationship(back_populates="faces")

