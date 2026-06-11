"""Runtime configuration persisted in the local database."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SchedulerRuntimeConfig(Base):
    __tablename__ = "scheduler_runtime_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 통합 동기화(스캔+이미지 AI) 자동 실행 토글. NULL은 기본값(켜짐)으로 본다.
    sync_enabled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_sync_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
