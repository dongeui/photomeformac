"""Scan trigger endpoints."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_state
from app.core.settings import AppSettings
from app.core.contracts import ProcessingJobState
from app.models.job import ProcessingJob
from app.services.processing.pipeline import LibraryJobBusyError


router = APIRouter(tags=["scan"])


@router.post("/scan")
async def trigger_scan(
    request: Request,
    full_scan: bool = Query(default=False),
    retry_errors_only: bool = Query(default=False),
    source_root: Optional[List[str]] = Query(default=None),
    source_roots: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    pipeline = require_state(request, "pipeline")
    settings = require_state(request, "settings")
    requested_roots = _parse_source_roots(settings, source_root=source_root, source_roots=source_roots)
    try:
        # 스캔 전체가 끝날 때까지 도는 동기 작업 — 이벤트 루프에서 직접 돌리면
        # 메뉴바 폴링을 포함한 서버 전체가 멈추므로 워커 스레드로 내린다.
        summary = await run_in_threadpool(
            lambda: pipeline.submit_scan_job(
                full_scan=full_scan,
                run_now=True,
                trigger="api",
                retry_errors_only=retry_errors_only,
                source_roots=requested_roots,
            )
        )
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)
    return {"job": asdict(summary)}


@router.post("/scan/async", status_code=status.HTTP_202_ACCEPTED)
async def trigger_scan_async(
    request: Request,
    background_tasks: BackgroundTasks,
    full_scan: bool = Query(default=False),
    retry_errors_only: bool = Query(default=False),
    source_root: Optional[List[str]] = Query(default=None),
    source_roots: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    pipeline = require_state(request, "pipeline")
    settings = require_state(request, "settings")
    requested_roots = _parse_source_roots(settings, source_root=source_root, source_roots=source_roots)
    try:
        # 제출 자체는 짧지만 submit 락이 진행 중인 스캔에 잡혀 있을 수 있다
        # (최대 락 타임아웃만큼 대기) — 이벤트 루프를 막지 않게 워커로 내린다.
        summary = await run_in_threadpool(
            lambda: pipeline.submit_scan_job(
                full_scan=full_scan,
                run_now=False,
                trigger="api-async",
                retry_errors_only=retry_errors_only,
                source_roots=requested_roots,
            )
        )
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)
    except OperationalError as exc:
        _raise_job_submission_busy(exc)
    background_tasks.add_task(pipeline.run_scan_job, summary.job_id)
    return {"job": asdict(summary)}


@router.post("/scan/retry-errors/async", status_code=status.HTTP_202_ACCEPTED)
async def trigger_scan_retry_errors_async(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    pipeline = require_state(request, "pipeline")
    try:
        summary = await run_in_threadpool(
            lambda: pipeline.submit_scan_job(
                full_scan=False,
                retry_errors_only=True,
                run_now=False,
                trigger="api-async-retry-errors",
            )
        )
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)
    except OperationalError as exc:
        _raise_job_submission_busy(exc)
    background_tasks.add_task(pipeline.run_scan_job, summary.job_id)
    return {"job": asdict(summary)}


@router.get("/scan/jobs/{job_id}")
async def read_scan_job(request: Request, job_id: str) -> dict[str, Any]:
    database = require_state(request, "database")
    with database.session_factory() as session:
        job = session.get(ProcessingJob, job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan job not found")
        return {
            "job": {
                "job_id": job.id,
                "job_kind": job.job_kind,
                "status": job.status,
                "payload": job.payload_json,
                "result": job.result_json,
                "error_stage": job.error_stage,
                "error_message": job.error_message,
                "attempts": job.attempts,
                "enqueued_at": job.enqueued_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "updated_at": job.updated_at,
            }
        }


@router.post("/scan/semantic-backfill")
async def trigger_semantic_backfill(
    request: Request,
    batch_size: int = Query(default=50, ge=1, le=10000),
) -> dict[str, Any]:
    """Generate CLIP embeddings for any media that missed the semantic pass."""
    pipeline = require_state(request, "pipeline")
    try:
        result = await run_in_threadpool(pipeline.run_semantic_backfill, batch_size=batch_size)
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)
    return result


@router.post("/scan/semantic-backfill/async", status_code=status.HTTP_202_ACCEPTED)
async def trigger_semantic_backfill_async(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_size: int = Query(default=50, ge=1, le=10000),
) -> dict[str, Any]:
    pipeline = require_state(request, "pipeline")
    try:
        summary = await run_in_threadpool(
            lambda: pipeline.submit_semantic_backfill_job(
                batch_size=batch_size,
                run_now=False,
                trigger="api-async",
            )
        )
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)
    except OperationalError as exc:
        _raise_job_submission_busy(exc)
    background_tasks.add_task(pipeline.run_semantic_job, summary.job_id)
    return {"job": asdict(summary)}


@router.post("/scan/semantic-maintenance")
async def trigger_semantic_maintenance(
    request: Request,
    batch_size: int = Query(default=1000, ge=1, le=10000),
) -> dict[str, Any]:
    """Refresh Phase 2 search documents for only stale or missing rows."""
    pipeline = require_state(request, "pipeline")
    try:
        return await run_in_threadpool(pipeline.run_semantic_maintenance, batch_size=batch_size)
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)


@router.post("/scan/semantic-maintenance/async", status_code=status.HTTP_202_ACCEPTED)
async def trigger_semantic_maintenance_async(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_size: int = Query(default=1000, ge=1, le=10000),
) -> dict[str, Any]:
    pipeline = require_state(request, "pipeline")
    try:
        summary = await run_in_threadpool(
            lambda: pipeline.submit_semantic_maintenance_job(
                batch_size=batch_size,
                run_now=False,
                trigger="api-async",
            )
        )
    except LibraryJobBusyError as exc:
        _raise_active_job_conflict(exc)
    except OperationalError as exc:
        _raise_job_submission_busy(exc)
    background_tasks.add_task(pipeline.run_semantic_job, summary.job_id)
    return {"job": asdict(summary)}


@router.post("/scan/repair-metadata")
async def trigger_repair_metadata(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_size: int = Query(default=500, ge=1, le=50000),
) -> dict[str, Any]:
    """Re-extract EXIF GPS from image files that were scanned without it.

    Targets formats like HEIC that require pillow-heif.  Files not accessible
    from the current process (NAS not mounted) are counted in skipped_no_file.
    Runs in the background; returns immediately with a confirmation message.
    """
    pipeline = require_state(request, "pipeline")
    background_tasks.add_task(pipeline.repair_missing_gps, batch_size=batch_size)
    return {"status": "started", "batch_size": batch_size, "message": "GPS repair running in background"}


@router.post("/scan/repair-exif-datetime")
async def trigger_repair_exif_datetime(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_size: int = Query(default=2000, ge=1, le=100000),
) -> dict[str, Any]:
    """Backfill exif_datetime for images that have none stored.

    Targets HEIC/JPG/DNG files where exif_datetime is NULL — typically files
    scanned before pillow-heif support was enabled.  Runs in the background.
    """
    pipeline = require_state(request, "pipeline")
    background_tasks.add_task(pipeline.repair_missing_exif_datetime, batch_size=batch_size)
    return {"status": "started", "batch_size": batch_size, "message": "exif_datetime repair running in background"}


@router.post("/scan/jobs/{job_id}/cancel", status_code=200)
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    """Cancel a running library job. The job loop checks for this and exits cleanly."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        job = session.get(ProcessingJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status not in (ProcessingJobState.RUNNING.value, ProcessingJobState.QUEUED.value):
            return {"job_id": job_id, "status": job.status, "message": "Job is not running"}
        job.status = ProcessingJobState.CANCELED.value
        session.commit()
    return {"job_id": job_id, "status": ProcessingJobState.CANCELED.value, "message": "Cancellation requested"}


def _parse_source_roots(
    settings: AppSettings,
    *,
    source_root: Optional[List[str]],
    source_roots: Optional[str],
) -> tuple[Path, ...] | None:
    raw_values: list[str] = []
    if source_root:
        raw_values.extend(source_root)
    if source_roots:
        raw_values.extend(source_roots.replace("\n", ",").split(","))

    resolved_roots: list[Path] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        cleaned = raw_value.strip()
        if not cleaned:
            continue
        path = _resolve_source_root_input(cleaned, settings)
        key = str(path)
        if key in seen:
            continue
        if not _path_exists(path):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_missing_source_root_detail(path, settings),
            )
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Source root is not a directory: {path}",
            )
        seen.add(key)
        resolved_roots.append(path)

    return tuple(resolved_roots) if resolved_roots else None


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _resolve_source_root_input(raw_value: str, settings: AppSettings) -> Path:
    candidate = Path(raw_value).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if os.path.exists("/.dockerenv"):
        mapped = _map_host_source_to_runtime_path(resolved, settings)
        if mapped is not None:
            try:
                mapped_resolved = mapped.resolve()
            except OSError:
                mapped_resolved = mapped
            if _path_exists(mapped_resolved):
                return mapped_resolved
    if _path_exists(resolved):
        return resolved

    mapped = _map_host_source_to_runtime_path(resolved, settings)
    if mapped is not None:
        try:
            mapped_resolved = mapped.resolve()
        except OSError:
            mapped_resolved = mapped
        if _path_exists(mapped_resolved):
            return mapped_resolved
    return resolved


def _map_host_source_to_runtime_path(path: Path, settings: AppSettings) -> Path | None:
    host_root = settings.source_root_host
    mount_root = settings.source_root_mount
    if host_root is None or mount_root is None:
        return None
    try:
        relative = path.relative_to(host_root)
    except ValueError:
        return None
    return (mount_root / relative).expanduser()


def _missing_source_root_detail(path: Path, settings: AppSettings) -> str:
    message = f"Source root does not exist: {path}"
    if os.path.exists("/.dockerenv"):
        if settings.source_root_host is not None and settings.source_root_mount is not None:
            return (
                f"{message}. Trove can auto-map Finder/NAS paths under {settings.source_root_host} "
                f"to the Docker mount {settings.source_root_mount}, but this path was still not found "
                "after mapping. Check that the NAS share is mounted on macOS and that Docker Compose "
                "points to the same host root."
            )
        return (
            f"{message}. The server is running in Docker, so Finder/host NAS paths are not visible "
            "unless they are mounted into the container. Mount the NAS share on macOS first, then set "
            "TROVE_SOURCE_ROOT to that host path and TROVE_SOURCE_MOUNT to the path Trove should "
            "see inside Docker."
        )
    if str(path).startswith("/Volumes/"):
        return (
            f"{message}. On macOS, Finder Network entries are not usable until the share is actually "
            "mounted under /Volumes. Open the NAS share in Finder or mount it with smb://, then retry."
        )
    return message


def _raise_job_submission_busy(exc: OperationalError) -> None:
    message = str(getattr(exc, "orig", exc)).lower()
    if "database is locked" in message:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another library job is still writing to the catalog. Wait a moment and try again.",
        ) from exc
    raise exc


def _raise_active_job_conflict(exc: LibraryJobBusyError) -> None:
    active = exc.active_job
    kind = str(active.get("job_kind") or "job")
    status_name = str(active.get("status") or "queued")
    if kind == "scan":
        detail = "Phase 1 scan is already active. Wait for it to finish before starting Phase 2."
    elif kind in {"semantic_backfill", "semantic_maintenance"}:
        detail = "Phase 2 semantic work is already active. Wait for it to finish before starting Phase 1."
    else:
        detail = f"Another library job is active ({kind}/{status_name}). Wait for it to finish."
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    ) from exc
