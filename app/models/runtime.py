"""Runtime configuration persisted in the local database."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SchedulerRuntimeConfig(Base):
    __tablename__ = "scheduler_runtime_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    phase1_interval_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    phase2_interval_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_phase1_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_phase2_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
