"""Filesystem observation ledger used for the stability gate and retryable failures."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ScanObservation(Base):
    __tablename__ = "scan_observations"
    __table_args__ = (
        UniqueConstraint("current_path", name="uq_scan_observation_current_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_root: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    current_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting_stable")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    stable_after_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    error_count: Mapped[int] = mapped_column(nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

