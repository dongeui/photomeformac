"""Tag model for media filtering."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        Index("ix_tags_type_value", "tag_type", "tag_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("media_files.file_id", ondelete="CASCADE"), index=True)
    tag_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tag_value: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    media_file: Mapped["MediaFile"] = relationship(back_populates="tags")

