"""단일 통합 동기화(스캔+이미지 AI) 스케줄러.

짧은 주기(기본 600초)로 깨어나 '할 일이 있는지'(파일 변화 stat 프로브 또는
남은 이미지 AI 백로그)를 저비용으로 확인하고, 있을 때만 통합 스캔 잡
(스캔 → 이미지 AI 드레인)을 제출한다. 사용자에게 보이는 작업 종류는
"동기화" 하나다 — 별도의 유휴 이미지 AI 스케줄은 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import threading
from typing import Optional

from app.core.settings import AppSettings
from app.models.runtime import SchedulerRuntimeConfig
from app.services.nas_remount import NasRemounter
from app.services.processing.pipeline import ProcessingPipeline
from app.services.processing.pipeline import LibraryJobBusyError
from sqlalchemy.orm import Session, sessionmaker


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerSnapshot:
    enabled: bool
    running: bool
    sync_auto_enabled: bool
    sync_interval_seconds: int
    last_sync_run_at: Optional[datetime]
    next_sync_run_at: Optional[datetime]


_NAS_PING_INTERVAL_SECONDS = 120


class SchedulerService:
    def __init__(self, settings: AppSettings, pipeline: ProcessingPipeline, session_factory: sessionmaker[Session]) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._session_factory = session_factory
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # NAS keep-alive state: maps source_root str → last known availability
        self._nas_available: dict[str, bool] = {}
        self._last_nas_ping_at: datetime | None = None
        # 마운트가 살아있을 때 smb/afp URL을 기록해 두고, 끊기면 자동 재마운트.
        self._nas_remounter = NasRemounter(settings.data_root)

    @property
    def enabled(self) -> bool:
        config = self._load_runtime_config()
        return self._sync_enabled(config)

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="trove-scheduler", daemon=True)
        self._thread.start()
        self._running = True
        logger.info("scheduler started")

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._running = False
        logger.info("scheduler stopped")

    def tick(self, now: datetime | None = None) -> SchedulerSnapshot:
        now = now or datetime.utcnow()
        config = self._load_runtime_config()
        if self._is_sync_due(config, now):
            try:
                if self._pipeline.has_pending_sync_work():
                    self._pipeline.submit_scan_job(full_scan=True, run_now=True, trigger="scheduler-sync")
                # 할 일이 없어도 틱은 소비한다 — 프로브 주기를 유지.
                self._set_last_sync_run(now)
            except LibraryJobBusyError:
                logger.debug("sync scheduler skipped: library job already active")
            except Exception:
                logger.exception("sync scheduler tick failed")
        return self.snapshot(now)

    def snapshot(self, now: datetime | None = None) -> SchedulerSnapshot:
        now = now or datetime.utcnow()
        config = self._load_runtime_config()
        return SchedulerSnapshot(
            enabled=self._sync_enabled(config),
            running=self._running,
            sync_auto_enabled=self._sync_auto_enabled(config),
            sync_interval_seconds=self._sync_interval_seconds(),
            last_sync_run_at=config.last_sync_run_at,
            next_sync_run_at=self._next_sync_run_at(config, now),
        )

    def set_sync_auto(self, enabled: bool) -> SchedulerSnapshot:
        """대시보드의 자동 동기화 토글."""
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            config.sync_enabled = enabled
            session.commit()
        return self.snapshot()

    def _run_loop(self) -> None:
        # Allow NAS bind mounts to settle before the first scan
        self._stop_event.wait(30.0)
        while not self._stop_event.is_set():
            try:
                now = datetime.utcnow()
                if (
                    self._last_nas_ping_at is None
                    or (now - self._last_nas_ping_at).total_seconds() >= _NAS_PING_INTERVAL_SECONDS
                ):
                    self._ping_source_roots(now)
                self.tick(now)
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop_event.wait(1.0)

    def _ping_source_roots(self, now: datetime) -> None:
        """Ping each source root to keep the SMB/NAS connection alive.

        If a root was previously unavailable and is now reachable, submit an
        incremental scan immediately so freshly-reconnected files are picked up.
        """
        self._last_nas_ping_at = now
        reconnected_roots: list[Path] = []
        for source_root in self._settings.source_roots:
            key = str(source_root)
            was_available = self._nas_available.get(key, True)
            try:
                list(os.scandir(source_root))
                now_available = True
            except OSError:
                now_available = False

            self._nas_available[key] = now_available
            if now_available and not was_available:
                logger.info("NAS source root reconnected", extra={"source_root": key})
                reconnected_roots.append(source_root)
            if now_available:
                # 살아있는 동안 마운트 URL을 기록해 둔다(끊겼을 때 재마운트용).
                self._nas_remounter.record_url_for_root(key)
            else:
                logger.warning("NAS source root unreachable", extra={"source_root": key})
                # Finder를 열지 않아도 다시 붙도록 기록된 URL로 재마운트 시도(스로틀).
                self._nas_remounter.try_remount(key, now)

        if reconnected_roots:
            try:
                self._pipeline.submit_scan_job(full_scan=False, run_now=True, trigger="nas-reconnect")
            except LibraryJobBusyError:
                pass

    def nas_status(self) -> dict[str, bool]:
        return dict(self._nas_available)

    def _load_runtime_config(self) -> SchedulerRuntimeConfig:
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            if session.new:
                session.commit()
                session.refresh(config)
            session.expunge(config)
            return config

    def _ensure_runtime_config_row(self, session: Session) -> SchedulerRuntimeConfig:
        config = session.get(SchedulerRuntimeConfig, 1)
        if config is None:
            config = SchedulerRuntimeConfig(id=1)
            session.add(config)
            session.flush()
        return config

    def _sync_auto_enabled(self, config: SchedulerRuntimeConfig) -> bool:
        # NULL(미설정)은 켜짐 — 앱은 기본적으로 알아서 동기화한다.
        return config.sync_enabled is None or bool(config.sync_enabled)

    def _sync_enabled(self, config: SchedulerRuntimeConfig) -> bool:
        return self._settings.sync_scheduler_enabled and self._sync_auto_enabled(config)

    def _sync_interval_seconds(self) -> int:
        return max(60, self._settings.sync_scheduler_interval_seconds)

    def _next_sync_run_at(self, config: SchedulerRuntimeConfig, now: datetime) -> datetime | None:
        if not self._sync_enabled(config):
            return None
        if config.last_sync_run_at is None:
            return now
        return config.last_sync_run_at + timedelta(seconds=self._sync_interval_seconds())

    def _is_sync_due(self, config: SchedulerRuntimeConfig, now: datetime) -> bool:
        next_run = self._next_sync_run_at(config, now)
        return next_run is not None and now >= next_run

    def _set_last_sync_run(self, now: datetime) -> None:
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            config.last_sync_run_at = now
            session.commit()
