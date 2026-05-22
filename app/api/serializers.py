"""Response serializers for media, jobs, and status payloads."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.models.job import ProcessingJob
from app.models.media import MediaFile
from app.scheduler.service import SchedulerSnapshot


def serialize_media_file(media_file: MediaFile) -> dict[str, Any]:
    duration_ms = None
    if media_file.duration_seconds is not None:
        duration_ms = int(round(media_file.duration_seconds * 1000))
    return {
        "file_id": media_file.file_id,
        "current_path": media_file.current_path,
        "filename": media_file.filename,
        "source_root": media_file.source_root,
        "relative_path": media_file.relative_path,
        "media_kind": media_file.media_kind,
        "media_type": media_file.media_kind,
        "status": media_file.status,
        "size_bytes": media_file.size_bytes,
        "mtime_ns": media_file.mtime_ns,
        "partial_hash": media_file.partial_hash,
        "content_hash": media_file.content_hash,
        "fingerprint_version": media_file.fingerprint_version,
        "width": media_file.width,
        "height": media_file.height,
        "duration_seconds": media_file.duration_seconds,
        "duration_ms": duration_ms,
        "codec_name": media_file.codec_name,
        "mime_type": media_file.mime_type,
        "exif_datetime": media_file.exif_datetime,
        "metadata_json": media_file.metadata_json,
        "stable_after_at": media_file.stable_after_at,
        "error_stage": media_file.error_stage,
        "error_message": media_file.error_message,
        "error_count": media_file.error_count,
        "processed_at": media_file.processed_at,
        "first_seen_at": media_file.first_seen_at,
        "last_seen_at": media_file.last_seen_at,
        "updated_at": media_file.updated_at,
    }


def serialize_processing_job(job: ProcessingJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_kind": job.job_kind,
        "status": job.status,
        "payload_json": job.payload_json,
        "result_json": job.result_json,
        "error_stage": job.error_stage,
        "error_message": job.error_message,
        "attempts": job.attempts,
        "enqueued_at": job.enqueued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
    }


def serialize_scheduler_snapshot(snapshot: SchedulerSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
