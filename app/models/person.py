"""Person model for future face grouping."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 병합되어 숨겨진 사람이 어느 target에 합쳐졌는지(soft-hide). NULL이면 일반 사람.
    # 삭제하지 않고 숨기므로 unmerge 시 이름/별칭이 그대로 복원된다.
    merged_into_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True, index=True
    )

    faces: Mapped[list["Face"]] = relationship(back_populates="person", foreign_keys="Face.person_id")
