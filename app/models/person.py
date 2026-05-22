"""Person model for future face grouping."""

from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    faces: Mapped[list["Face"]] = relationship(back_populates="person")
