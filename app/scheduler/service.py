"""Local scheduler primitives for serialized Phase 1/2 scheduling."""

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
SCHEDULE_OPTIONS_HOURS: tuple[int | None, ...] = (None, 6, 12, 24)


@dataclass(frozen=True)
class SchedulerSnapshot:
    enabled: bool
    running: bool
    poll_interval_seconds: int
    semantic_scheduler_enabled: bool
    semantic_scheduler_interval_seconds: int
    phase1_interval_hours: int | None
    phase2_interval_hours: int | None
    last_poll_at: Optional[datetime]
    last_full_scan_at: Optional[datetime]
    next_poll_at: Optional[datetime]
    next_full_scan_at: Optional[datetime]
    last_semantic_maintenance_at: Optional[datetime]
    next_semantic_maintenance_at: Optional[datetime]
    library_interval_hours: int | None
    last_library_run_at: Optional[datetime]
    next_library_run_at: Optional[datetime]
    background_task_kind: str | None = None
    background_task_state: str | None = None
    background_task_started_at: Optional[datetime] = None
    background_task_message: str | None = None


_NAS_PING_INTERVAL_SECONDS = 120


class SchedulerService:
    def __init__(self, settings: AppSettings, pipeline: ProcessingPipeline, session_factory: sessionmaker[Session]) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._session_factory = session_factory
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._background_task_lock = threading.Lock()
        self._background_task: dict[str, object] | None = None
        # NAS keep-alive state: maps source_root str → last known availability
        self._nas_available: dict[str, bool] = {}
        self._last_nas_ping_at: datetime | None = None
        # 마운트가 살아있을 때 smb/afp URL을 기록해 두고, 끊기면 자동 재마운트.
        self._nas_remounter = NasRemounter(settings.data_root)

    @property
    def enabled(self) -> bool:
        config = self._load_runtime_config()
        return self._library_interval_hours(config) is not None

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="photome-scheduler", daemon=True)
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
        if self._is_phase1_due(config, now):
            try:
                self._pipeline.submit_scan_job(full_scan=True, run_now=True, trigger="scheduler-library")
                self._set_last_library_run(now)
            except LibraryJobBusyError:
                logger.debug("library scheduler skipped: library job already active")
            except Exception:
                logger.exception("library scheduler tick failed")
        elif self._is_idle_semantic_due(config, now):
            try:
                if self._pipeline.has_semantic_maintenance_work():
                    self._set_background_task(
                        kind="semantic_maintenance",
                        state="running",
                        started_at=now,
                        message="이미지 AI 누락분을 뒤에서 분석 중",
                    )
                    try:
                        self._pipeline.run_semantic_maintenance(
                            batch_size=self._settings.semantic_maintenance_batch_size,
                        )
                    finally:
                        self._clear_background_task()
                self._set_last_phase2_run(now)
            except LibraryJobBusyError:
                logger.debug("semantic scheduler skipped: library job already active")
            except Exception:
                logger.exception("semantic scheduler tick failed")
        return self.snapshot(now)

    def snapshot(self, now: datetime | None = None) -> SchedulerSnapshot:
        now = now or datetime.utcnow()
        config = self._load_runtime_config()
        background_task = self._current_background_task()
        background_task_kind: str | None = background_task.get("kind") if background_task else None  # type: ignore[assignment]
        background_task_state: str | None = background_task.get("state") if background_task else None  # type: ignore[assignment]
        background_task_started_at: datetime | None = background_task.get("started_at") if background_task else None  # type: ignore[assignment]
        background_task_message: str | None = background_task.get("message") if background_task else None  # type: ignore[assignment]
        if not isinstance(background_task_kind, str):
            background_task_kind = None
        if not isinstance(background_task_state, str):
            background_task_state = None
        if not isinstance(background_task_started_at, datetime):
            background_task_started_at = None
        if not isinstance(background_task_message, str):
            background_task_message = None
        return SchedulerSnapshot(
            enabled=self._library_interval_hours(config) is not None,
            running=self._running,
            poll_interval_seconds=self._settings.scheduler_poll_interval_seconds,
            semantic_scheduler_enabled=self._settings.semantic_scheduler_enabled,
            semantic_scheduler_interval_seconds=self._settings.semantic_scheduler_interval_seconds,
            phase1_interval_hours=config.phase1_interval_hours,
            phase2_interval_hours=config.phase2_interval_hours,
            last_poll_at=None,
            last_full_scan_at=config.last_phase1_run_at,
            next_poll_at=None,
            next_full_scan_at=self._next_phase1_run_at(config, now),
            last_semantic_maintenance_at=config.last_phase2_run_at,
            next_semantic_maintenance_at=self._next_phase2_run_at(config, now),
            library_interval_hours=self._library_interval_hours(config),
            last_library_run_at=self._last_library_run_at(config),
            next_library_run_at=self._next_library_run_at(config, now),
            background_task_kind=background_task_kind,
            background_task_state=background_task_state,
            background_task_started_at=background_task_started_at,
            background_task_message=background_task_message,
        )

    def _set_background_task(
        self,
        *,
        kind: str,
        state: str,
        started_at: datetime,
        message: str,
    ) -> None:
        with self._background_task_lock:
            self._background_task = {
                "kind": kind,
                "state": state,
                "started_at": started_at,
                "message": message,
            }

    def _clear_background_task(self) -> None:
        with self._background_task_lock:
            self._background_task = None

    def _current_background_task(self) -> dict[str, object] | None:
        with self._background_task_lock:
            return dict(self._background_task) if self._background_task is not None else None

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

    def cycle_phase_schedule(self, phase: str) -> SchedulerSnapshot:
        now = datetime.utcnow()
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            if phase in {"library", "phase1"}:
                current = self._library_interval_hours(config)
                next_value = self._cycle_value(current)
                config.phase1_interval_hours = next_value
                config.phase2_interval_hours = None
                if current is None and next_value is not None:
                    config.last_phase1_run_at = now
            elif phase == "phase2":
                current = config.phase2_interval_hours
                next_value = self._cycle_value(current)
                config.phase2_interval_hours = next_value
                if current is None and next_value is not None:
                    config.last_phase2_run_at = now
            else:
                raise ValueError(f"Unknown phase: {phase}")
            session.commit()
        return self.snapshot()

    def _cycle_value(self, current: int | None) -> int | None:
        index = SCHEDULE_OPTIONS_HOURS.index(current) if current in SCHEDULE_OPTIONS_HOURS else 0
        return SCHEDULE_OPTIONS_HOURS[(index + 1) % len(SCHEDULE_OPTIONS_HOURS)]

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

    def _is_phase1_due(self, config: SchedulerRuntimeConfig, now: datetime) -> bool:
        next_run = self._next_phase1_run_at(config, now)
        return next_run is not None and now >= next_run

    def _is_phase2_due(self, config: SchedulerRuntimeConfig, now: datetime) -> bool:
        next_run = self._next_phase2_run_at(config, now)
        return next_run is not None and now >= next_run

    def _is_idle_semantic_due(self, config: SchedulerRuntimeConfig, now: datetime) -> bool:
        if self._is_phase2_due(config, now):
            return True
        if not self._settings.semantic_scheduler_enabled:
            return False
        last_run = config.last_phase2_run_at
        if last_run is None:
            return True
        return now >= last_run + timedelta(seconds=max(60, self._settings.semantic_scheduler_interval_seconds))

    def _next_phase1_run_at(self, config: SchedulerRuntimeConfig, now: datetime) -> datetime | None:
        if config.phase1_interval_hours is None:
            return None
        if config.last_phase1_run_at is None:
            return now
        return config.last_phase1_run_at + timedelta(hours=max(1, config.phase1_interval_hours))

    def _next_phase2_run_at(self, config: SchedulerRuntimeConfig, now: datetime) -> datetime | None:
        if config.phase2_interval_hours is None:
            return None
        if config.last_phase2_run_at is None:
            return now
        return config.last_phase2_run_at + timedelta(hours=max(1, config.phase2_interval_hours))

    def _set_last_phase1_run(self, now: datetime) -> None:
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            config.last_phase1_run_at = now
            session.commit()

    def _set_last_phase2_run(self, now: datetime) -> None:
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            config.last_phase2_run_at = now
            session.commit()

    def _library_interval_hours(self, config: SchedulerRuntimeConfig) -> int | None:
        if config.phase1_interval_hours is not None:
            return config.phase1_interval_hours
        return config.phase2_interval_hours

    def _last_library_run_at(self, config: SchedulerRuntimeConfig) -> datetime | None:
        return config.last_phase1_run_at or config.last_phase2_run_at

    def _next_library_run_at(self, config: SchedulerRuntimeConfig, now: datetime) -> datetime | None:
        hours = self._library_interval_hours(config)
        if hours is None:
            return None
        last_run = self._last_library_run_at(config)
        if last_run is None:
            return now
        return last_run + timedelta(hours=max(1, hours))

    def _is_library_due(self, config: SchedulerRuntimeConfig, now: datetime) -> bool:
        next_run = self._next_library_run_at(config, now)
        return next_run is not None and now >= next_run

    def _set_last_library_run(self, now: datetime) -> None:
        with self._session_factory() as session:
            config = self._ensure_runtime_config_row(session)
            config.last_phase1_run_at = now
            session.commit()
