"""Local-first durable processing pipeline orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import logging
import math
import time
import unicodedata
from pathlib import Path
from shutil import which
from threading import Lock
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Iterator
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.contracts import DerivedAssetKind, MediaFaceInput, MediaKind, MediaTagInput, ProcessingJobKind, ProcessingJobState
from app.core.settings import asset_worker_cap
from app.models.face import Face
from app.models.job import ProcessingJob
from app.models.media import MediaFile
from app.models.person import Person
from app.models.semantic import MediaCaption, MediaEmbedding, MediaOCR
from app.models.tag import Tag
from app.services.analysis import FaceAnalysisError, FaceAnalysisService
from app.services.analysis import auto_tags, image_signals
from app.services.caption import CaptionProvider
from app.services.caption.registry import get_caption_provider
from app.services.embedding import clip as clip_embedding
from app.services.search.hybrid import clear_query_cache
from app.services.search.vector import invalidate_global_vector_index
from app.services.geocoding import GeocodingProvider, NominatimProvider
from app.services.geocoding.cached import CachedGeocodingService
from app.services.fingerprint.service import FingerprintService
from app.services.metadata.service import MetadataService
from app.services.ocr import OCRResult, extract as extract_ocr
from app.services.processing.incremental import IncrementalScanService, IncrementalScanSummary
from app.services.processing.person_labels import (
    find_files_needing_person_label_sync,
    person_label_values,
    reconcile_file_person_tags,
)
from app.services.processing.registry import MediaCatalog, build_derived_asset_location
from app.services.scanner.service import DirMtimeCache, ScannerService
from app.services.semantic import SemanticCatalog
from app.services.thumbnail.service import ThumbnailService
from app.services.video.service import VideoKeyframeService


logger = logging.getLogger(__name__)
PLACE_TAG_TYPES = frozenset({"place", "location", "place_detail", "geo", "geo_detail"})
PERSON_TAG_TYPES = frozenset({"person", "people", "face"})
SEMANTIC_MAINTENANCE_BATCH_SIZE = 500
SEMANTIC_MANUAL_BATCH_SIZE = 1000
LIBRARY_SUBMIT_LOCK_TIMEOUT_SECONDS = 5.0


def _add_stage_timing(timings: dict[str, float], stage: str, seconds: float) -> None:
    timings[stage] = timings.get(stage, 0.0) + max(0.0, seconds)


def _stage_timing_summary(timings: dict[str, float]) -> dict[str, Any]:
    total = sum(max(0.0, value) for value in timings.values())
    return {
        "total_seconds": round(total, 3),
        "stages": {
            stage: {
                "seconds": round(seconds, 3),
                "percent": round((seconds / total) * 100, 1) if total > 0 else 0.0,
            }
            for stage, seconds in sorted(timings.items())
        },
    }


def _merge_timing_dicts(target: dict[str, float], source: dict[str, Any]) -> None:
    for stage, seconds in source.items():
        try:
            _add_stage_timing(target, str(stage), float(seconds))
        except (TypeError, ValueError):
            continue


def _merge_media_batches(*batches: list[MediaFile], limit: int) -> list[MediaFile]:
    merged: list[MediaFile] = []
    seen: set[str] = set()
    for batch in batches:
        for media_file in batch:
            if media_file.file_id in seen:
                continue
            seen.add(media_file.file_id)
            merged.append(media_file)
            if len(merged) >= limit:
                return merged
    return merged


@dataclass(frozen=True)
class PipelineSummary:
    job_id: str
    job_kind: str
    status: str
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    error_stage: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class FaceMaterializationResult:
    faces: tuple[MediaFaceInput, ...]
    person_tags: tuple[MediaTagInput, ...]
    summaries: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()


@dataclass
class PersonCentroidState:
    person: Person
    centroid: tuple[float, ...]
    sample_count: int
    relative_path: Path


class ProcessingPipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        scanner: ScannerService,
        fingerprint_service: FingerprintService,
        metadata_service: MetadataService,
        thumbnail_service: ThumbnailService,
        keyframe_service: VideoKeyframeService,
        *,
        face_analysis_service: FaceAnalysisService | None = None,
        derived_root: Path | None = None,
        embeddings_root: Path | None = None,
        face_match_threshold: float = 0.363,
        face_analysis_version: str = "face-v1",
        place_tag_precision: int = 3,
        semantic_ocr_enabled: bool = True,
        semantic_ocr_heuristic_enabled: bool = True,
        semantic_clip_enabled: bool = False,
        semantic_ocr_version: str = "ocr-v1",
        semantic_embedding_version: str = "embedding-v1",
        semantic_auto_tag_version: str = "auto-v1",
        semantic_search_version: str = "search-v1",
        semantic_caption_version: str = "caption-v1",
        caption_provider: CaptionProvider | None = None,
        geocoding_provider: GeocodingProvider | None = None,
        geocoding_enabled: bool = False,
        asset_processing_workers: int = 1,
        semantic_maintenance_batch_size: int = SEMANTIC_MAINTENANCE_BATCH_SIZE,
        semantic_manual_batch_size: int = SEMANTIC_MANUAL_BATCH_SIZE,
        dir_mtime_cache: DirMtimeCache | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._scanner = scanner
        self._fingerprint_service = fingerprint_service
        self._metadata_service = metadata_service
        self._thumbnail_service = thumbnail_service
        self._keyframe_service = keyframe_service
        self._face_analysis_service = face_analysis_service
        self._derived_root = (derived_root or Path("./derived_root")).expanduser().resolve()
        self._embeddings_root = (embeddings_root or (self._derived_root / "embeddings")).expanduser().resolve()
        self._face_match_threshold = max(-1.0, min(1.0, face_match_threshold))
        self._face_analysis_version = face_analysis_version
        self._place_tag_precision = max(0, place_tag_precision)
        self._semantic_ocr_enabled = semantic_ocr_enabled
        self._semantic_ocr_heuristic_enabled = semantic_ocr_heuristic_enabled
        self._semantic_clip_enabled = semantic_clip_enabled
        self._semantic_ocr_version = semantic_ocr_version
        self._semantic_embedding_version = semantic_embedding_version
        self._semantic_auto_tag_version = semantic_auto_tag_version
        self._semantic_search_version = semantic_search_version
        self._semantic_caption_version = semantic_caption_version
        self._caption_provider: CaptionProvider | None = caption_provider if caption_provider is not None else get_caption_provider()
        self._geocoding_enabled = geocoding_enabled
        self._geocoding_provider: GeocodingProvider = geocoding_provider or NominatimProvider()
        self._asset_processing_workers = max(1, min(asset_worker_cap(), int(asset_processing_workers or 1)))
        self._semantic_maintenance_batch_size = max(50, min(5000, int(semantic_maintenance_batch_size or SEMANTIC_MAINTENANCE_BATCH_SIZE)))
        self._semantic_manual_batch_size = max(50, min(5000, int(semantic_manual_batch_size or SEMANTIC_MANUAL_BATCH_SIZE)))
        # `or` 금지: DirMtimeCache는 __len__이 있어 비어 있으면 falsy다.
        # 첫 부팅(빈 캐시)에 영속화가 붙은 인스턴스를 버리는 사고가 났었다.
        self._dir_mtime_cache = dir_mtime_cache if dir_mtime_cache is not None else DirMtimeCache()
        self._semantic_maintenance_lock = Lock()
        self._library_submit_lock = Lock()

    @contextmanager
    def _library_submit_guard(self) -> Iterator[None]:
        # run_now=True 제출(스케줄러 라이브러리 스캔 등)은 작업 전체가 끝날
        # 때까지 이 락을 쥔다. 그동안 들어온 제출이 무기한 대기하면 호출자
        # (async 핸들러면 이벤트 루프 전체)가 같이 멈추므로, 짧게 기다린 뒤
        # busy로 응답한다.
        if not self._library_submit_lock.acquire(timeout=LIBRARY_SUBMIT_LOCK_TIMEOUT_SECONDS):
            with self._session_factory() as session:
                active = self._active_library_job(session)
            raise LibraryJobBusyError(active or {"job_kind": "scan", "status": ProcessingJobState.RUNNING.value})
        try:
            yield
        finally:
            self._library_submit_lock.release()

    def submit_scan_job(
        self,
        *,
        full_scan: bool = False,
        run_now: bool = True,
        trigger: str = "manual",
        retry_errors_only: bool = False,
        run_semantic_maintenance: bool = True,
        source_roots: tuple[Path, ...] | None = None,
    ) -> PipelineSummary:
        payload: dict[str, Any] = {
            "full_scan": full_scan,
            "trigger": trigger,
            "retry_errors_only": retry_errors_only,
            "run_semantic_maintenance": run_semantic_maintenance,
        }
        if source_roots is not None:
            payload["source_roots"] = [str(path) for path in source_roots]

        with self._library_submit_guard():
            with self._session_factory() as session:
                self._ensure_no_active_library_job(session)
                job = ProcessingJob(
                    job_kind=ProcessingJobKind.SCAN.value,
                    status=ProcessingJobState.QUEUED.value,
                    payload_json=payload,
                    attempts=0,
                )
                session.add(job)
                session.flush()

                if run_now:
                    try:
                        self._run_scan_job(
                            session,
                            job,
                            full_scan=full_scan,
                            retry_errors_only=retry_errors_only,
                            run_semantic_maintenance=run_semantic_maintenance,
                            source_roots=source_roots,
                        )
                    except Exception:
                        session.commit()
                        return self._to_summary(job)

                session.commit()
                return self._to_summary(job)

    def run_scan_job(self, job_id: str) -> PipelineSummary:
        with self._session_factory() as session:
            job = session.get(ProcessingJob, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            if job.job_kind != ProcessingJobKind.SCAN.value:
                raise ValueError(f"Job {job_id} is not a scan job")
            payload = job.payload_json or {}
            full_scan = bool(payload.get("full_scan"))
            retry_errors_only = bool(payload.get("retry_errors_only"))
            run_semantic_maintenance = bool(payload.get("run_semantic_maintenance", True))
            source_roots = _coerce_source_roots(payload.get("source_roots"))
            try:
                self._run_scan_job(
                    session,
                    job,
                    full_scan=full_scan,
                    retry_errors_only=retry_errors_only,
                    run_semantic_maintenance=run_semantic_maintenance,
                    source_roots=source_roots,
                )
            except Exception:
                session.commit()
                return self._to_summary(job)
            session.commit()
            return self._to_summary(job)

    def submit_semantic_backfill_job(
        self,
        *,
        batch_size: int = 50,
        run_now: bool = True,
        trigger: str = "manual",
    ) -> PipelineSummary:
        payload: dict[str, Any] = {"batch_size": batch_size, "trigger": trigger}
        with self._library_submit_guard():
            with self._session_factory() as session:
                self._ensure_no_active_library_job(session)
                job = ProcessingJob(
                    job_kind=ProcessingJobKind.SEMANTIC_BACKFILL.value,
                    status=ProcessingJobState.QUEUED.value,
                    payload_json=payload,
                    attempts=0,
                )
                session.add(job)
                session.flush()

                if run_now:
                    try:
                        self._run_semantic_job(session, job, batch_size=batch_size, mode="backfill")
                    except Exception:
                        session.commit()
                        return self._to_summary(job)

                session.commit()
                return self._to_summary(job)

    def submit_semantic_maintenance_job(
        self,
        *,
        batch_size: int | None = None,
        run_now: bool = True,
        trigger: str = "manual",
    ) -> PipelineSummary:
        resolved_batch_size = int(batch_size or self._semantic_manual_batch_size)
        payload: dict[str, Any] = {"batch_size": resolved_batch_size, "trigger": trigger}
        with self._library_submit_guard():
            with self._session_factory() as session:
                self._ensure_no_active_library_job(session)
                job = ProcessingJob(
                    job_kind=ProcessingJobKind.SEMANTIC_MAINTENANCE.value,
                    status=ProcessingJobState.QUEUED.value,
                    payload_json=payload,
                    attempts=0,
                )
                session.add(job)
                session.flush()

                if run_now:
                    try:
                        self._run_semantic_job(session, job, batch_size=resolved_batch_size, mode="maintenance")
                    except Exception:
                        session.commit()
                        return self._to_summary(job)

                session.commit()
                return self._to_summary(job)

    def run_semantic_job(self, job_id: str) -> PipelineSummary:
        with self._session_factory() as session:
            job = session.get(ProcessingJob, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            payload = job.payload_json or {}
            batch_size = int(payload.get("batch_size") or self._semantic_manual_batch_size)
            if job.job_kind == ProcessingJobKind.SEMANTIC_BACKFILL.value:
                mode = "backfill"
            elif job.job_kind == ProcessingJobKind.SEMANTIC_MAINTENANCE.value:
                mode = "maintenance"
            else:
                raise ValueError(f"Job {job_id} is not a semantic job")
            try:
                self._run_semantic_job(session, job, batch_size=batch_size, mode=mode)
            except Exception:
                session.commit()
                return self._to_summary(job)
            session.commit()
            return self._to_summary(job)

    def rebuild_media_assets(self, file_id: str) -> PipelineSummary:
        with self._session_factory() as session:
            job = ProcessingJob(
                job_kind=ProcessingJobKind.PIPELINE.value,
                status=ProcessingJobState.RUNNING.value,
                payload_json={"file_id": file_id, "asset_refresh": True},
                attempts=1,
                started_at=datetime.utcnow(),
            )
            session.add(job)
            session.flush()

            catalog = MediaCatalog(session)
            media_file = catalog.get_media(file_id)
            if media_file is None:
                job.status = ProcessingJobState.FAILED.value
                job.error_stage = "catalog"
                job.error_message = "media file not found"
                job.finished_at = datetime.utcnow()
                session.commit()
                return self._to_summary(job)

            try:
                item_started_at = time.perf_counter()
                result = self._refresh_media_assets(session, media_file)
                _merge_timing_dicts(timings, result.get("timings", {}))
                _add_stage_timing(timings, "item_total", time.perf_counter() - item_started_at)
                job.status = ProcessingJobState.SUCCEEDED.value
                job.result_json = result
                job.finished_at = datetime.utcnow()
            except Exception as exc:
                logger.exception("failed to refresh media assets", extra={"file_id": file_id})
                job.status = ProcessingJobState.FAILED.value
                job.error_stage = "asset_pipeline"
                job.error_message = str(exc)
                job.finished_at = datetime.utcnow()

            session.commit()
            return self._to_summary(job)

    def recover_interrupted_library_jobs(self) -> dict[str, int]:
        with self._session_factory() as session:
            interrupted = session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.job_kind.in_(LIBRARY_JOB_KINDS),
                    ProcessingJob.status.in_((ProcessingJobState.QUEUED.value, ProcessingJobState.RUNNING.value)),
                )
            ).scalars().all()
            if not interrupted:
                return {"recovered": 0}

            now = datetime.utcnow()
            for job in interrupted:
                payload = dict(job.result_json or {})
                payload["progress"] = {
                    "stage": "interrupted",
                    "message": "Interrupted by restart. Run again to resume from current catalog state.",
                    "resume_supported": True,
                }
                job.status = ProcessingJobState.CANCELED.value
                job.error_stage = "interrupted"
                job.error_message = "Interrupted by restart. Run again to resume from current catalog state."
                job.finished_at = now
                job.result_json = payload
            try:
                session.commit()
                return {"recovered": len(interrupted)}
            except OperationalError:
                session.rollback()
                return {"recovered": 0, "skipped": len(interrupted), "reason": "database_locked"}

    def run_semantic_backfill(
        self,
        *,
        batch_size: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Alias for run_semantic_maintenance — single unified Phase 2 pass."""
        return self.run_semantic_maintenance(batch_size=batch_size, progress_callback=progress_callback)

    def _active_source_roots_nfc(self) -> set[str]:
        """현재 동기화 대상 소스 루트(NFC 정규화). 폴더 전환 시 옛 루트의
        사진을 missing 대신 archived로 보존하는 판정에 쓴다."""
        return {
            unicodedata.normalize("NFC", str(root))
            for root in self._scanner.config.source_roots
        }

    def run_semantic_maintenance(
        self,
        *,
        batch_size: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Refresh Phase 2 search documents only for media that need it.

        The scheduler calls this in cycles. A non-blocking lock prevents two
        cycles from rebuilding the same semantic rows concurrently.
        """
        if not self._semantic_maintenance_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "already_running", "pending": 0, "succeeded": 0, "failed": 0}

        resolved_batch_size = int(batch_size or self._semantic_maintenance_batch_size)
        batch_size = resolved_batch_size
        try:
            with self._session_factory() as session:
                semantic_catalog = SemanticCatalog(session)
                catalog = MediaCatalog(session)
                # 임베딩 누락분을 배치 앞에 먼저 채운다. 임베딩은 사용자에게
                # 보이는 "이미지 AI" 진행률이라, 기존 임베딩의 태그/검색문서
                # 갱신(수만 건)이 큐를 다 차지하면 며칠씩 진행률이 0으로 보인다.
                pending: list[MediaFile] = []
                if self._semantic_clip_enabled:
                    pending = catalog.list_media_needing_embedding(
                        limit=batch_size,
                        model_name=self._clip_model_identifier(),
                        version=self._semantic_embedding_version,
                    )
                search_doc_pending_count = 0
                if len(pending) < batch_size:
                    search_doc_batch = semantic_catalog.list_media_needing_search_document(
                        version=self._semantic_search_version,
                        limit=batch_size,
                        auto_tag_version=self._semantic_auto_tag_version if self._semantic_clip_enabled else None,
                    )
                    search_doc_pending_count = len(search_doc_batch)
                    pending = _merge_media_batches(pending, search_doc_batch, limit=batch_size)
                if self._face_analysis_service is not None and len(pending) < batch_size:
                    face_pending = session.scalars(
                        select(MediaFile)
                        .where(
                            MediaFile.status.in_(("thumb_done", "analysis_done")),
                            MediaFile.media_kind == "image",
                            or_(
                                MediaFile.face_version.is_(None),
                                MediaFile.face_version != self._face_analysis_version,
                            ),
                        )
                        .limit(batch_size - len(pending))
                    ).all()
                    pending = _merge_media_batches(pending, list(face_pending), limit=batch_size)
                pending_ids = [media_file.file_id for media_file in pending]

            succeeded = failed = embeddings_created = auto_tag_files = auto_tag_values = search_documents_updated = faces_reanalyzed = 0
            if progress_callback is not None:
                progress_callback({
                    "mode": "maintenance",
                    "pending": len(pending_ids),
                    "current": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "batch_size": batch_size,
                    "embeddings_created": 0,
                    "auto_tag_files": 0,
                    "auto_tag_values": 0,
                    "search_documents_updated": 0,
                    "faces_reanalyzed": 0,
                })

            for index, file_id in enumerate(pending_ids, start=1):
                with self._session_factory() as session:
                    try:
                        semantic_catalog = SemanticCatalog(session)
                        catalog = MediaCatalog(session)
                        media_file = catalog.get_media(file_id)
                        if media_file is None:
                            failed += 1
                            continue
                        if not Path(media_file.current_path).exists():
                            # 원본이 사라진 파일은 상태 전이로 큐에서 내보낸다.
                            # 그대로 두면 매 사이클 같은 파일이 실패를 반복한다.
                            catalog.retire_missing_source(
                                media_file.file_id,
                                source_root=media_file.source_root,
                                active_source_roots=self._active_source_roots_nfc(),
                                now=datetime.utcnow(),
                            )
                            session.commit()
                            continue
                        self._try_repair_gps(media_file)
                        self._refresh_place_tags(session, media_file, catalog)
                        if (
                            self._face_analysis_service is not None
                            and media_file.media_kind == "image"
                            and media_file.face_version != self._face_analysis_version
                        ):
                            self._refresh_faces_phase2(session, media_file, catalog)
                            faces_reanalyzed += 1
                        if self._semantic_clip_enabled:
                            embedding_result = self._ensure_clip_embedding(session, media_file, catalog, semantic_catalog)
                            if embedding_result and embedding_result.get("_created"):
                                embeddings_created += 1
                        refreshed_tags = self._refresh_auto_tags_from_existing_embedding(session, media_file)
                        if refreshed_tags:
                            auto_tag_files += 1
                            auto_tag_values += len(refreshed_tags)
                        semantic_catalog.upsert_search_document(media_file, version=self._semantic_search_version)
                        search_documents_updated += 1
                        session.commit()
                        succeeded += 1
                    except Exception as exc:
                        session.rollback()
                        logger.warning(
                            "semantic maintenance failed",
                            extra={"file_id": file_id, "error": str(exc)},
                        )
                        failed += 1

                if progress_callback is not None and (index == 1 or index == len(pending_ids) or index % 25 == 0):
                    progress_callback({
                        "mode": "maintenance",
                        "pending": len(pending_ids),
                        "current": index,
                        "succeeded": succeeded,
                        "failed": failed,
                        "batch_size": batch_size,
                        "embeddings_created": embeddings_created,
                        "auto_tag_files": auto_tag_files,
                        "auto_tag_values": auto_tag_values,
                        "search_documents_updated": search_documents_updated,
                        "faces_reanalyzed": faces_reanalyzed,
                        "face_analysis_available": self._face_analysis_service is not None,
                        "clip_enabled": self._semantic_clip_enabled,
                    })

            # Invalidate caches so new content is immediately queryable
            if succeeded > 0:
                cleared = clear_query_cache()
                logger.debug("semantic maintenance cleared %d cached queries", cleared)
                if invalidate_global_vector_index():
                    logger.debug("semantic maintenance invalidated FAISS index")
                # Invalidate tag vocabulary cache so new place/person tags
                # are immediately recognised in query planning
                from app.services.search.vocab import TagVocabularyCache
                TagVocabularyCache.invalidate()

            # has_more=True only when there are more files to process AND we made real progress.
            # Without the embeddings_created guard, files that need CLIP but can't get it
            # (model unavailable) keep filling the batch forever via list_media_needing_embedding —
            # 임베딩 우선 선정에서는 그런 항목이 배치 전체를 차지하므로 이 가드가 무한 재시도를 끊는다.
            batch_full = len(pending_ids) == batch_size
            real_progress = search_doc_pending_count > 0 or embeddings_created > 0 or faces_reanalyzed > 0
            return {
                "skipped": False,
                "pending": len(pending_ids),
                "succeeded": succeeded,
                "failed": failed,
                "has_more": batch_full and real_progress,
                "version": self._semantic_search_version,
                "embeddings_created": embeddings_created,
                "auto_tag_files": auto_tag_files,
                "auto_tag_values": auto_tag_values,
                "search_documents_updated": search_documents_updated,
                "faces_reanalyzed": faces_reanalyzed,
                "face_analysis_available": self._face_analysis_service is not None,
                "clip_enabled": self._semantic_clip_enabled,
            }
        finally:
            self._semantic_maintenance_lock.release()

    def _run_semantic_job(
        self,
        session: Session,
        job: ProcessingJob,
        *,
        batch_size: int,
        mode: str,
    ) -> dict[str, Any]:
        now = datetime.utcnow()
        job.status = ProcessingJobState.RUNNING.value
        job.started_at = job.started_at or now
        job.attempts = (job.attempts or 0) + 1
        job.error_stage = None
        job.error_message = None
        self._set_job_progress(
            session,
            job,
            stage="collecting",
            message="Collecting semantic work items.",
            details={"mode": mode, "batch_size": batch_size, "full_run": True},
        )
        session.commit()

        try:
            result = self._run_semantic_full_job(session, job, batch_size=batch_size, mode=mode)
        except Exception as exc:
            logger.exception("semantic job failed", extra={"job_id": job.id, "mode": mode})
            job.status = ProcessingJobState.FAILED.value
            job.error_stage = f"semantic_{mode}"
            job.error_message = str(exc)
            job.finished_at = datetime.utcnow()
            session.commit()
            raise

        job.status = ProcessingJobState.SUCCEEDED.value
        job.result_json = dict(result)
        job.result_json["progress"] = {
            "stage": "complete",
            "message": "Semantic job complete.",
            "mode": mode,
            "batch_size": batch_size,
            "pending": result.get("pending", 0),
            "succeeded": result.get("succeeded", 0),
            "failed": result.get("failed", 0),
        }
        job.finished_at = datetime.utcnow()
        session.commit()
        return result

    def _run_semantic_full_job(
        self,
        session: Session,
        job: ProcessingJob,
        *,
        batch_size: int,
        mode: str,
    ) -> dict[str, Any]:
        aggregate: dict[str, Any] = {
            "skipped": False,
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "has_more": False,
            "chunks": 0,
            "batch_size": batch_size,
            "full_run": True,
            "embeddings_created": 0,
            "auto_tag_files": 0,
            "auto_tag_values": 0,
            "search_documents_updated": 0,
            "faces_reanalyzed": 0,
        }

        def merge_result(result: dict[str, Any]) -> None:
            aggregate["chunks"] += 1
            aggregate["pending"] += int(result.get("pending") or 0)
            aggregate["succeeded"] += int(result.get("succeeded") or 0)
            aggregate["failed"] += int(result.get("failed") or 0)
            aggregate["has_more"] = bool(result.get("has_more"))
            aggregate["version"] = result.get("version") or aggregate.get("version")
            aggregate["reason"] = result.get("reason") or aggregate.get("reason")
            for key in ("embeddings_created", "auto_tag_files", "auto_tag_values", "search_documents_updated", "faces_reanalyzed"):
                aggregate[key] += int(result.get(key) or 0)

        while True:
            chunk_index = int(aggregate["chunks"]) + 1

            def progress(payload: dict[str, Any]) -> None:
                details = {
                    **payload,
                    "full_run": True,
                    "chunk": chunk_index,
                    "total_succeeded": aggregate["succeeded"] + int(payload.get("succeeded") or 0),
                    "total_failed": aggregate["failed"] + int(payload.get("failed") or 0),
                    "total_embeddings_created": aggregate["embeddings_created"] + int(payload.get("embeddings_created") or 0),
                    "total_auto_tag_files": aggregate["auto_tag_files"] + int(payload.get("auto_tag_files") or 0),
                    "total_auto_tag_values": aggregate["auto_tag_values"] + int(payload.get("auto_tag_values") or 0),
                    "total_search_documents_updated": aggregate["search_documents_updated"] + int(payload.get("search_documents_updated") or 0),
                    "total_faces_reanalyzed": aggregate["faces_reanalyzed"] + int(payload.get("faces_reanalyzed") or 0),
                }
                self._set_job_progress(
                    session,
                    job,
                    stage="processing",
                    message="Refreshing full semantic library." if mode == "maintenance" else "Generating full semantic library.",
                    details=details,
                    commit=True,
                )

            if mode == "backfill":
                result = self.run_semantic_backfill(batch_size=batch_size, progress_callback=progress)
            elif mode == "maintenance":
                result = self.run_semantic_maintenance(batch_size=batch_size, progress_callback=progress)
            else:
                raise ValueError(f"Unknown semantic job mode: {mode}")

            merge_result(result)
            if not result.get("has_more"):
                break
            if int(result.get("succeeded") or 0) == 0:
                aggregate["stopped_reason"] = "no_successful_items_in_chunk"
                break
            # Allow external cancellation: check if job was marked cancelled in DB
            session.expire(job)
            if job.status not in (ProcessingJobState.RUNNING.value, ProcessingJobState.QUEUED.value):
                aggregate["stopped_reason"] = "cancelled"
                break

        return aggregate

    def status_snapshot(self) -> dict[str, Any]:
        with self._session_factory() as session:
            catalog = MediaCatalog(session)
            job_counts = self._job_counts(session)
            active_job = self._active_library_job(session)
            return {
                "media": {
                    "total": catalog.count_media(),
                    "status_counts": catalog.media_status_counts(),
                    "kind_counts": catalog.media_kind_counts(),
                    "errors": catalog.count_media(status="error"),
                    "waiting_stable": catalog.count_observations(status="waiting_stable"),
                    "missing": catalog.count_media(status="missing"),
                    "observations": catalog.observation_status_counts(),
                },
                "jobs": {
                    **job_counts,
                    "active_library_job": active_job,
                },
                "runtime": {
                    "asset_processing_workers": self._asset_processing_workers,
                    "semantic_maintenance_batch_size": self._semantic_maintenance_batch_size,
                    "semantic_manual_batch_size": self._semantic_manual_batch_size,
                },
            }

    def update_resource_settings(
        self,
        *,
        asset_processing_workers: int | None = None,
        semantic_maintenance_batch_size: int | None = None,
        semantic_manual_batch_size: int | None = None,
    ) -> None:
        if asset_processing_workers is not None:
            self._asset_processing_workers = max(1, min(asset_worker_cap(), int(asset_processing_workers)))
        if semantic_maintenance_batch_size is not None:
            self._semantic_maintenance_batch_size = max(50, min(5000, int(semantic_maintenance_batch_size)))
        if semantic_manual_batch_size is not None:
            self._semantic_manual_batch_size = max(50, min(5000, int(semantic_manual_batch_size)))

    def has_active_library_job(self) -> bool:
        with self._session_factory() as session:
            return self._active_library_job(session) is not None

    def has_pending_sync_work(self) -> bool:
        """유휴 틱에서 '통합 동기화를 돌릴 이유가 있는지' 저비용 판정.

        파일 변화는 디렉터리 stat 스윕(스캔 없음), 이미지 AI 백로그는
        limit-1 쿼리만 본다. 스케줄러가 주기마다 호출한다.
        """
        if self._scanner.has_changes():
            return True
        return self.has_semantic_maintenance_work()

    def has_semantic_maintenance_work(self) -> bool:
        """Return True when idle image-AI/search maintenance has useful work."""
        with self._session_factory() as session:
            semantic_catalog = SemanticCatalog(session)
            catalog = MediaCatalog(session)
            if semantic_catalog.list_media_needing_search_document(
                version=self._semantic_search_version,
                limit=1,
                auto_tag_version=self._semantic_auto_tag_version if self._semantic_clip_enabled else None,
            ):
                return True
            if self._semantic_clip_enabled and catalog.list_media_needing_embedding(
                limit=1,
                model_name=self._clip_model_identifier(),
                version=self._semantic_embedding_version,
            ):
                return True
            if self._face_analysis_service is not None:
                face_pending = session.scalar(
                    select(MediaFile.file_id)
                    .where(
                        MediaFile.status.in_(("thumb_done", "analysis_done")),
                        MediaFile.media_kind == "image",
                        or_(
                            MediaFile.face_version.is_(None),
                            MediaFile.face_version != self._face_analysis_version,
                        ),
                    )
                    .limit(1)
                )
                return face_pending is not None
            return False

    def _run_scan_job(
        self,
        session: Session,
        job: ProcessingJob,
        *,
        full_scan: bool,
        retry_errors_only: bool = False,
        run_semantic_maintenance: bool = True,
        source_roots: tuple[Path, ...] | None = None,
    ) -> IncrementalScanSummary:
        now = datetime.utcnow()
        catalog = MediaCatalog(session)
        excluded_media = catalog.exclude_media_kind("video", now=now)
        job.status = ProcessingJobState.RUNNING.value
        job.started_at = job.started_at or now
        job.attempts = (job.attempts or 0) + 1
        job.error_stage = None
        job.error_message = None
        self._set_job_progress(
            session,
            job,
            stage="retrying_errors" if retry_errors_only else "scanning",
            message="Retrying failed media." if retry_errors_only else "Scanning source roots.",
            details={
                "full_scan": full_scan,
                "retry_errors_only": retry_errors_only,
                "run_semantic_maintenance": run_semantic_maintenance,
                "excluded_media": excluded_media,
                "source_roots": [str(path) for path in source_roots] if source_roots is not None else None,
            },
        )
        session.commit()

        semantic_summary: dict[str, Any] | None = None
        try:
            if retry_errors_only:
                scan_summary = IncrementalScanSummary(scanned=0, created=0, updated=0, moved=0, missing=0, failed=0)
            else:
                scanner = self._scanner.with_source_roots(source_roots) if source_roots is not None else self._scanner

                def scan_progress(payload: dict[str, Any]) -> None:
                    stage = str(payload.get("stage") or "scanning")
                    message = str(payload.get("message") or "Scanning source roots.")
                    self._set_job_progress(
                        session,
                        job,
                        stage=stage,
                        message=message,
                        details={
                            "full_scan": full_scan,
                            "retry_errors_only": retry_errors_only,
                            "run_semantic_maintenance": run_semantic_maintenance,
                            "source_roots": [str(path) for path in source_roots] if source_roots is not None else None,
                            **payload,
                        },
                        commit=True,
                    )

                scan_summary = IncrementalScanService(
                    scanner,
                    self._fingerprint_service,
                    self._metadata_service,
                    dir_mtime_cache=self._dir_mtime_cache,
                ).run(session, progress_callback=scan_progress)
            self._set_job_progress(
                session,
                job,
                stage="processing_assets",
                message="Retrying failed media assets." if retry_errors_only else "Refreshing thumbnails and semantic assets.",
                details={
                    "full_scan": full_scan,
                    "retry_errors_only": retry_errors_only,
                    "run_semantic_maintenance": run_semantic_maintenance,
                    "source_roots": [str(path) for path in source_roots] if source_roots is not None else None,
                    "summary": asdict(scan_summary),
                },
            )
            session.commit()
            # full_scan의 실제 의미: walk는 어차피 dir mtime 캐시를 쓰므로
            # "전체를 다시 읽는다"가 아니라 "missing 상태도 재처리 대상에
            # 포함한다"뿐이다. (missing 부활 재확인은 스캔 자체가 항상 수행)
            processing_statuses = (
                ("error",)
                if retry_errors_only
                else (("metadata_done", "error", "missing") if full_scan else ("metadata_done", "error"))
            )
            processed_summary = self._process_pending_media(
                session,
                trigger_job_id=job.id,
                parent_job=job,
                statuses=processing_statuses,
            )
            if run_semantic_maintenance and not retry_errors_only:
                self._set_job_progress(
                    session,
                    job,
                    stage="semantic_maintenance",
                    message="Refreshing search and AI analysis.",
                    details={
                        "full_scan": full_scan,
                        "retry_errors_only": retry_errors_only,
                        "run_semantic_maintenance": run_semantic_maintenance,
                        "summary": asdict(scan_summary),
                        "processed": processed_summary,
                    },
                )
                session.commit()
                semantic_summary = self._run_scan_semantic_followup(session, job, batch_size=self._semantic_maintenance_batch_size)
        except Exception as exc:
            logger.exception("scan job failed", extra={"job_id": job.id})
            job.status = ProcessingJobState.FAILED.value
            job.error_stage = "scan"
            job.error_message = str(exc)
            job.finished_at = datetime.utcnow()
            session.commit()
            raise

        job.status = ProcessingJobState.SUCCEEDED.value
        job.result_json = {
            "full_scan": full_scan,
            "retry_errors_only": retry_errors_only,
            "run_semantic_maintenance": run_semantic_maintenance,
            "source_roots": [str(path) for path in source_roots] if source_roots is not None else None,
            "summary": asdict(scan_summary),
            "processed": processed_summary,
            "semantic": semantic_summary,
            "progress": {
                "stage": "complete",
                "message": "Retry complete." if retry_errors_only else "Library sync complete.",
                "retry_errors_only": retry_errors_only,
                "run_semantic_maintenance": run_semantic_maintenance,
                "summary": asdict(scan_summary),
                "processed": processed_summary,
                "semantic": semantic_summary,
            },
        }
        job.finished_at = datetime.utcnow()
        session.commit()
        return scan_summary

    def _run_scan_semantic_followup(
        self,
        session: Session,
        job: ProcessingJob,
        *,
        batch_size: int,
    ) -> dict[str, Any]:
        aggregate: dict[str, Any] = {
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "chunks": 0,
            "embeddings_created": 0,
            "auto_tag_files": 0,
            "auto_tag_values": 0,
            "search_documents_updated": 0,
            "faces_reanalyzed": 0,
            "full_run": True,
            "batch_size": batch_size,
        }

        def merge_result(result: dict[str, Any]) -> None:
            aggregate["chunks"] += 1
            aggregate["pending"] += int(result.get("pending") or 0)
            aggregate["succeeded"] += int(result.get("succeeded") or 0)
            aggregate["failed"] += int(result.get("failed") or 0)
            aggregate["has_more"] = bool(result.get("has_more"))
            aggregate["version"] = result.get("version") or aggregate.get("version")
            for key in ("embeddings_created", "auto_tag_files", "auto_tag_values", "search_documents_updated", "faces_reanalyzed"):
                aggregate[key] += int(result.get(key) or 0)

        while True:
            chunk_index = int(aggregate["chunks"]) + 1

            def progress(payload: dict[str, Any]) -> None:
                details = {
                    **payload,
                    "full_run": True,
                    "chunk": chunk_index,
                    "total_succeeded": aggregate["succeeded"] + int(payload.get("succeeded") or 0),
                    "total_failed": aggregate["failed"] + int(payload.get("failed") or 0),
                    "total_embeddings_created": aggregate["embeddings_created"] + int(payload.get("embeddings_created") or 0),
                    "total_auto_tag_files": aggregate["auto_tag_files"] + int(payload.get("auto_tag_files") or 0),
                    "total_auto_tag_values": aggregate["auto_tag_values"] + int(payload.get("auto_tag_values") or 0),
                    "total_search_documents_updated": aggregate["search_documents_updated"] + int(payload.get("search_documents_updated") or 0),
                    "total_faces_reanalyzed": aggregate["faces_reanalyzed"] + int(payload.get("faces_reanalyzed") or 0),
                }
                self._set_job_progress(
                    session,
                    job,
                    stage="semantic_maintenance",
                    message="Refreshing search and AI analysis.",
                    details=details,
                    commit=True,
                )

            result = self.run_semantic_maintenance(batch_size=batch_size, progress_callback=progress)
            merge_result(result)
            if not result.get("has_more"):
                break
            if int(result.get("succeeded") or 0) == 0:
                aggregate["stopped_reason"] = "no_successful_items_in_chunk"
                break
            session.expire(job)
            if job.status not in (ProcessingJobState.RUNNING.value, ProcessingJobState.QUEUED.value):
                aggregate["stopped_reason"] = "cancelled"
                break

        aggregate["person_labels"] = self._reconcile_person_label_tags(job)
        return aggregate

    def _reconcile_person_label_tags(
        self,
        job: ProcessingJob | None = None,
        *,
        max_files: int = 5000,
        chunk_size: int = 200,
    ) -> dict[str, Any]:
        """Back-fill person tags so already-indexed files carry the full label
        set (display name + aliases) of the people they contain.

        New files are tagged correctly on ingest by ``_person_tags_for_faces``;
        this heals files indexed before alias-tagging existed (and any later
        drift after a rename/merge). Bounded per sync via ``max_files`` so a
        large first run can span a few syncs — it is idempotent and finds
        nothing once drained.
        """
        scanned = synced = 0
        try:
            with self._session_factory() as session:
                candidates = find_files_needing_person_label_sync(session, limit=max_files)
            scanned = len(candidates)
            for start in range(0, len(candidates), chunk_size):
                chunk = candidates[start : start + chunk_size]
                with self._session_factory() as session:
                    semantic_catalog = SemanticCatalog(session)
                    for file_id in chunk:
                        try:
                            if reconcile_file_person_tags(
                                session,
                                file_id=file_id,
                                search_version=self._semantic_search_version,
                                semantic_catalog=semantic_catalog,
                            ):
                                synced += 1
                        except Exception as exc:  # noqa: BLE001 - keep draining the batch
                            logger.warning(
                                "person label reconcile failed",
                                extra={"file_id": file_id, "error": str(exc)},
                            )
                    session.commit()
        except Exception:
            logger.exception("person label reconciliation pass failed")
        if synced > 0:
            clear_query_cache()
            from app.services.search.vocab import TagVocabularyCache

            TagVocabularyCache.invalidate()
        return {"scanned": scanned, "synced": synced, "has_more": scanned >= max_files}

    def _process_pending_media(
        self,
        session: Session,
        *,
        trigger_job_id: str,
        parent_job: ProcessingJob | None = None,
        statuses: tuple[str, ...] = ("metadata_done",),
    ) -> dict[str, Any]:
        catalog = MediaCatalog(session)
        pending_media = catalog.list_media_for_processing(statuses=statuses)
        succeeded = 0
        failed = 0
        timings: dict[str, float] = {}
        total = len(pending_media)

        if parent_job is not None:
            self._set_job_progress(
                session,
                parent_job,
                stage="processing_assets",
                message="Preparing derived assets.",
                details={
                    "processed": {
                        "current": 0,
                        "total": total,
                        "succeeded": 0,
                        "failed": 0,
                    }
                },
            )
            session.commit()

        workers = self._asset_processing_workers if total > 1 else 1
        if workers > 1:
            pending_ids = [media_file.file_id for media_file in pending_media]
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trove-assets") as executor:
                futures = {
                    executor.submit(self._process_pending_media_item, file_id, trigger_job_id): file_id
                    for file_id in pending_ids
                }
                for index, future in enumerate(as_completed(futures), start=1):
                    should_commit_progress = index == 1 or index == total or index % 25 == 0
                    payload = future.result()
                    _merge_timing_dicts(timings, payload.get("timings", {}))
                    if payload.get("ok"):
                        succeeded += 1
                    else:
                        failed += 1
                    if parent_job is not None and should_commit_progress:
                        self._set_job_progress(
                            session,
                            parent_job,
                            stage="processing_assets",
                            message="Refreshing thumbnails and semantic assets.",
                            details={
                                "processed": {
                                    "current": index,
                                    "total": total,
                                    "succeeded": succeeded,
                                    "failed": failed,
                                },
                                "workers": workers,
                                "timings": _stage_timing_summary(timings),
                            },
                        )
                    if should_commit_progress:
                        session.commit()
        else:
            for index, media_file in enumerate(pending_media, start=1):
                should_commit_progress = index == 1 or index == total or index % 25 == 0
                payload = self._process_pending_media_item(media_file.file_id, trigger_job_id)
                _merge_timing_dicts(timings, payload.get("timings", {}))
                if payload.get("ok"):
                    succeeded += 1
                else:
                    failed += 1

                if parent_job is not None and should_commit_progress:
                    self._set_job_progress(
                        session,
                        parent_job,
                        stage="processing_assets",
                        message="Refreshing thumbnails and semantic assets.",
                        details={
                            "processed": {
                                "current": index,
                                "total": total,
                                "succeeded": succeeded,
                                "failed": failed,
                            },
                            "workers": workers,
                            "timings": _stage_timing_summary(timings),
                        },
                    )

                if should_commit_progress:
                    session.commit()

        return {
            "pending": len(pending_media),
            "succeeded": succeeded,
            "failed": failed,
            "workers": workers,
            "timings": _stage_timing_summary(timings),
        }

    def _process_pending_media_item(self, file_id: str, trigger_job_id: str) -> dict[str, Any]:
        timings: dict[str, float] = {}
        with self._session_factory() as worker_session:
            catalog = MediaCatalog(worker_session)
            media_file = worker_session.get(MediaFile, file_id)
            job = ProcessingJob(
                job_kind=ProcessingJobKind.PIPELINE.value,
                status=ProcessingJobState.RUNNING.value,
                payload_json={"file_id": file_id, "trigger_job_id": trigger_job_id},
                attempts=1,
                started_at=datetime.utcnow(),
            )
            item_started_at = time.perf_counter()
            try:
                if media_file is None:
                    raise ValueError(f"Unknown media file: {file_id}")
                result = self._refresh_media_assets(worker_session, media_file)
                _merge_timing_dicts(timings, result.get("timings", {}))
                _add_stage_timing(timings, "item_total", time.perf_counter() - item_started_at)
                job.status = ProcessingJobState.SUCCEEDED.value
                job.result_json = result
                job.finished_at = datetime.utcnow()
                worker_session.add(job)
                worker_session.commit()
                return {"ok": True, "file_id": file_id, "timings": timings}
            except Exception as exc:
                _add_stage_timing(timings, "failed", time.perf_counter() - item_started_at if "item_started_at" in locals() else 0.0)
                logger.exception("failed to refresh media assets", extra={"file_id": file_id})
                if media_file is not None:
                    catalog.mark_media_error(file_id, stage="asset_pipeline", message=str(exc), now=datetime.utcnow())
                job.status = ProcessingJobState.FAILED.value
                job.error_stage = "asset_pipeline"
                job.error_message = str(exc)
                job.finished_at = datetime.utcnow()
                worker_session.add(job)
                worker_session.commit()
                return {"ok": False, "file_id": file_id, "error": str(exc), "timings": timings}

    def _set_job_progress(
        self,
        session: Session,
        job: ProcessingJob,
        *,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
        commit: bool = False,
    ) -> None:
        current = dict(job.result_json or {})
        current["progress"] = {
            "stage": stage,
            "message": message,
            **(details or {}),
        }
        job.result_json = current
        session.flush()
        if commit:
            session.commit()

    def _existing_derived_asset_location(
        self,
        media_file: MediaFile,
        asset_kind: DerivedAssetKind,
        *,
        version: str = "v1",
        suffix: str = ".jpg",
    ):
        location = build_derived_asset_location(
            self._derived_root,
            asset_kind,
            media_file.file_id,
            version=version,
            suffix=suffix,
        )
        expected_path = str(location.relative_path)
        for asset in media_file.assets:
            if (
                asset.asset_kind == asset_kind.value
                and asset.asset_version == version
                and asset.derived_path == expected_path
                and location.absolute_path.is_file()
            ):
                return location
        return None

    def _should_extract_ocr(self, media_file: MediaFile, signal_payload: dict[str, Any], source_path: Path) -> bool:
        if not self._semantic_ocr_heuristic_enabled:
            return True
        lower_name = source_path.name.casefold()
        if any(token in lower_name for token in (
            "screenshot",
            "screen",
            "스크린샷",
            "scan",
            "document",
            "receipt",
            "invoice",
            "영수증",
            "문서",
        )):
            return True
        mime_type = (media_file.mime_type or "").casefold()
        if "png" in mime_type and float(signal_payload.get("edge_density") or 0.0) >= 0.025:
            return True
        edge_density = float(signal_payload.get("edge_density") or 0.0)
        brightness = float(signal_payload.get("brightness") or 0.0)
        return edge_density >= 0.08 or (edge_density >= 0.045 and brightness >= 0.5)

    def _current_ocr(self, session: Session, media_file: MediaFile) -> MediaOCR | None:
        row = session.get(MediaOCR, media_file.file_id)
        if row is None or row.version != self._semantic_ocr_version:
            return None
        return row

    def _current_caption(self, session: Session, media_file: MediaFile) -> MediaCaption | None:
        row = session.get(MediaCaption, media_file.file_id)
        if row is None or row.version != self._semantic_caption_version:
            return None
        return row

    def _refresh_media_assets(self, session: Session, media_file: MediaFile) -> dict[str, Any]:
        """Phase 1 asset pass: thumb, faces, OCR/CLIP semantics when enabled, search_document.

        CLIP embedding uses policy A: encode from ``current_path`` first, thumbnail fallback
        (see ``_materialize_clip_embedding``). Documented in docs/ops/RUNBOOK.md.
        """
        catalog = MediaCatalog(session)
        result: dict[str, Any] = {"file_id": media_file.file_id, "assets": []}
        timings: dict[str, float] = {}
        preserved_tags, existing_place_tags, existing_person_tags = self._split_existing_tags(media_file.tags)
        preserved_tags = [tag for tag in preserved_tags if tag.tag_type != "auto"]
        place_tags = self._materialize_place_tags(media_file, session=session)
        person_tags = existing_person_tags
        # Filename and datetime-derived auto tags (no ML, always fast)
        filename_tags = auto_tags.tags_from_filename(media_file.filename)
        datetime_tags = (
            auto_tags.tags_from_datetime(media_file.exif_datetime)
            if media_file.exif_datetime is not None
            else []
        )
        auto_tag_inputs: list[MediaTagInput] = list(filename_tags) + list(datetime_tags)
        face_result: FaceMaterializationResult | None = None
        analysis_warnings: list[str] = []
        media_kind = media_file.media_kind
        source_path = Path(media_file.current_path)

        stage_started_at = time.perf_counter()
        source_exists = source_path.exists()
        _add_stage_timing(timings, "source_exists", time.perf_counter() - stage_started_at)
        if not source_exists:
            outcome = catalog.retire_missing_source(
                media_file.file_id,
                source_root=media_file.source_root,
                active_source_roots=self._active_source_roots_nfc(),
                now=datetime.utcnow(),
            )
            result["skipped"] = {
                "reason": "source_missing" if outcome == "missing" else "inactive_source_root",
                "path": media_file.current_path,
            }
            result["timings"] = timings
            return result

        if media_kind == MediaKind.IMAGE.value:
            location = self._existing_derived_asset_location(media_file, DerivedAssetKind.THUMBNAIL, version="v1")
            if location is None:
                stage_started_at = time.perf_counter()
                location = self._thumbnail_service.generate(source_path, media_file.file_id, MediaKind.IMAGE)
                _add_stage_timing(timings, "thumbnail", time.perf_counter() - stage_started_at)
                stage_started_at = time.perf_counter()
                catalog.register_derived_asset(media_file.file_id, location.kind, location.relative_path)
                _add_stage_timing(timings, "db.assets", time.perf_counter() - stage_started_at)
                result["assets"].append({"kind": location.kind.value, "path": str(location.relative_path)})
            else:
                _add_stage_timing(timings, "thumbnail.cached", 0.0)
                result.setdefault("assets_skipped", []).append({
                    "kind": location.kind.value,
                    "path": str(location.relative_path),
                    "reason": "current",
                })

            if self._face_analysis_service is not None and media_file.face_version == self._face_analysis_version:
                _add_stage_timing(timings, "face_analysis.cached", 0.0)
                result["faces_skipped"] = {"reason": "current", "version": self._face_analysis_version}
            else:
                stage_started_at = time.perf_counter()
                face_result = self._materialize_faces(session, media_file)
                _add_stage_timing(timings, "face_analysis", time.perf_counter() - stage_started_at)
                if face_result is not None:
                    stage_started_at = time.perf_counter()
                    persisted_faces = catalog.upsert_faces(media_file.file_id, face_result.faces)
                    _add_stage_timing(timings, "db.faces", time.perf_counter() - stage_started_at)
                    person_tags = self._person_tags_for_faces(session, persisted_faces)
                    result["faces"] = list(face_result.summaries)
                    analysis_warnings.extend(face_result.warnings)
                if self._face_analysis_service is not None:
                    media_file.face_version = self._face_analysis_version
            semantic_result = self._materialize_image_semantics(session, media_file)
            if semantic_result:
                _merge_timing_dicts(timings, semantic_result.pop("_timings", {}))
                semantic_auto_tags = semantic_result.pop("_auto_tag_inputs", [])
                auto_tag_inputs = list(auto_tags.merge_auto_tags(auto_tag_inputs, semantic_auto_tags))
                result["semantic"] = semantic_result
            stage_started_at = time.perf_counter()
            catalog.set_media_status(media_file.file_id, status="thumb_done", now=datetime.utcnow())
            _add_stage_timing(timings, "db.status", time.perf_counter() - stage_started_at)

        elif media_kind == MediaKind.VIDEO.value:
            if which("ffmpeg") is None:
                result["assets_skipped"] = [
                    {
                        "kind": "video",
                        "reason": "ffmpeg_missing",
                    }
                ]
            else:
                thumb_location = self._thumbnail_service.generate(source_path, media_file.file_id, MediaKind.VIDEO)
                catalog.register_derived_asset(media_file.file_id, thumb_location.kind, thumb_location.relative_path)
                result["assets"].append({"kind": thumb_location.kind.value, "path": str(thumb_location.relative_path)})
                keyframe_locations = self._keyframe_service.extract(source_path, media_file.file_id)
                for location in keyframe_locations:
                    catalog.register_derived_asset(media_file.file_id, location.kind, location.relative_path)
                    result["assets"].append({"kind": location.kind.value, "path": str(location.relative_path)})
            catalog.set_media_status(media_file.file_id, status="analysis_done", now=datetime.utcnow())

        else:
            raise ValueError(f"Unsupported media kind: {media_kind}")

        stage_started_at = time.perf_counter()
        tags = catalog.upsert_tags(media_file.file_id, preserved_tags + place_tags + person_tags + auto_tag_inputs)
        _add_stage_timing(timings, "db.tags", time.perf_counter() - stage_started_at)
        if tags:
            result["tags"] = [{"type": tag.tag_type, "value": tag.tag_value} for tag in tags]
        elif place_tags or existing_place_tags or person_tags or existing_person_tags:
            result["tags"] = []

        stage_started_at = time.perf_counter()
        SemanticCatalog(session).upsert_search_document(media_file, version=self._semantic_search_version)
        _add_stage_timing(timings, "db.search_document", time.perf_counter() - stage_started_at)
        result["search_document"] = {"version": self._semantic_search_version}
        result["timings"] = timings

        if analysis_warnings:
            result["analysis_warnings"] = analysis_warnings
        return result

    def _materialize_image_semantics(self, session: Session, media_file: MediaFile) -> dict[str, Any]:
        semantic_catalog = SemanticCatalog(session)
        result: dict[str, Any] = {}
        timings: dict[str, float] = {}
        source_path = self._analysis_source_path(media_file)
        ocr_text = ""

        pre_ocr_signal_payload: dict[str, Any] | None = None
        if self._semantic_ocr_enabled:
            current_ocr = self._current_ocr(session, media_file)
            if current_ocr is not None:
                ocr_text = current_ocr.text_content
                _add_stage_timing(timings, "semantic.ocr.cached", 0.0)
                result["ocr"] = {
                    "engine": current_ocr.engine,
                    "text_length": len(current_ocr.text_content),
                    "blocks": 0,
                    "cached": True,
                }
            else:
                if self._semantic_ocr_heuristic_enabled:
                    stage_started_at = time.perf_counter()
                    pre_ocr_signal_payload = image_signals.extract_analysis(str(source_path), "")
                    _add_stage_timing(timings, "semantic.signals.pre_ocr", time.perf_counter() - stage_started_at)
                if pre_ocr_signal_payload is not None and not self._should_extract_ocr(media_file, pre_ocr_signal_payload, source_path):
                    ocr_result = OCRResult("", [], "skipped-heuristic")
                    stage_started_at = time.perf_counter()
                    semantic_catalog.upsert_ocr(media_file.file_id, ocr_result, version=self._semantic_ocr_version)
                    _add_stage_timing(timings, "db.ocr", time.perf_counter() - stage_started_at)
                    _add_stage_timing(timings, "semantic.ocr.skipped", 0.0)
                    result["ocr"] = {
                        "engine": ocr_result.engine,
                        "text_length": 0,
                        "blocks": 0,
                        "skipped": True,
                    }
                else:
                    stage_started_at = time.perf_counter()
                    ocr_result = extract_ocr(str(source_path))
                    _add_stage_timing(timings, "semantic.ocr", time.perf_counter() - stage_started_at)
                    stage_started_at = time.perf_counter()
                    semantic_catalog.upsert_ocr(media_file.file_id, ocr_result, version=self._semantic_ocr_version)
                    _add_stage_timing(timings, "db.ocr", time.perf_counter() - stage_started_at)
                    ocr_text = ocr_result.text
                    result["ocr"] = {
                        "engine": ocr_result.engine,
                        "text_length": len(ocr_result.text),
                        "blocks": len(ocr_result.blocks),
                    }

        if pre_ocr_signal_payload is not None and not ocr_text:
            signal_payload = pre_ocr_signal_payload
        else:
            stage_started_at = time.perf_counter()
            signal_payload = image_signals.extract_analysis(str(source_path), ocr_text)
            _add_stage_timing(timings, "semantic.signals", time.perf_counter() - stage_started_at)
        stage_started_at = time.perf_counter()
        semantic_catalog.upsert_analysis(media_file.file_id, signal_payload)
        _add_stage_timing(timings, "db.analysis", time.perf_counter() - stage_started_at)
        result["analysis"] = signal_payload
        signal_tags = auto_tags.tags_from_signals(signal_payload, ocr_text)
        embedding_tags: list[MediaTagInput] = []

        if self._semantic_clip_enabled:
            stage_started_at = time.perf_counter()
            embedding_result = self._ensure_clip_embedding(session, media_file, MediaCatalog(session), semantic_catalog)
            _add_stage_timing(
                timings,
                "semantic.clip" if embedding_result is None or embedding_result.get("_created") else "semantic.clip.cached",
                time.perf_counter() - stage_started_at,
            )
            if embedding_result:
                result["embedding"] = embedding_result
                embedding_tags = auto_tags.tags_from_embedding_file(
                    embedding_result["embedding_ref"],
                    self._embeddings_root,
                )

        if self._caption_provider is not None:
            current_caption = self._current_caption(session, media_file)
            if current_caption is not None:
                _add_stage_timing(timings, "semantic.caption.cached", 0.0)
                result["caption"] = {
                    "short_caption": current_caption.short_caption,
                    "objects": current_caption.objects_json,
                    "activities": current_caption.activities_json,
                    "setting": current_caption.setting,
                    "provider": current_caption.provider,
                    "cached": True,
                }
            else:
                stage_started_at = time.perf_counter()
                caption_result = self._caption_provider.caption(source_path)
                _add_stage_timing(timings, "semantic.caption", time.perf_counter() - stage_started_at)
                if caption_result is not None:
                    stage_started_at = time.perf_counter()
                    semantic_catalog.upsert_caption(
                        media_file.file_id,
                        caption_result,
                        version=self._semantic_caption_version,
                    )
                    _add_stage_timing(timings, "db.caption", time.perf_counter() - stage_started_at)
                    result["caption"] = {
                        "short_caption": caption_result.short_caption,
                        "objects": caption_result.objects,
                        "activities": caption_result.activities,
                        "setting": caption_result.setting,
                        "provider": caption_result.provider,
                    }

        stage_started_at = time.perf_counter()
        generated_tags = auto_tags.merge_auto_tags(signal_tags, embedding_tags)
        _add_stage_timing(timings, "semantic.auto_tags", time.perf_counter() - stage_started_at)
        stage_started_at = time.perf_counter()
        semantic_catalog.upsert_auto_tag_state(
            media_file.file_id,
            tags=generated_tags,
            version=self._semantic_auto_tag_version,
        )
        _add_stage_timing(timings, "db.auto_tag_state", time.perf_counter() - stage_started_at)
        if generated_tags:
            result["_auto_tag_inputs"] = generated_tags
            result["auto_tags"] = [
                {"type": tag.tag_type, "value": tag.tag_value}
                for tag in generated_tags
            ]

        result["_timings"] = timings
        return result

    def _ensure_clip_embedding(
        self,
        session: Session,
        media_file: MediaFile,
        catalog: MediaCatalog,
        semantic_catalog: SemanticCatalog,
    ) -> dict[str, Any] | None:
        """Return an existing current CLIP embedding or create one.

        Phase 2 maintenance uses this so enabling the local AI pack later will
        keep filling semantic image data without requiring keyword-specific
        manual jobs.
        """
        embedding = session.execute(
            select(MediaEmbedding)
            .where(
                MediaEmbedding.file_id == media_file.file_id,
                MediaEmbedding.model_name == self._clip_model_identifier(),
                MediaEmbedding.version == self._semantic_embedding_version,
            )
            .order_by(MediaEmbedding.updated_at.desc(), MediaEmbedding.id.desc())
        ).scalars().first()
        if embedding is not None:
            return {
                "model_name": embedding.model_name,
                "version": embedding.version,
                "embedding_ref": embedding.embedding_ref,
                "dimensions": embedding.dimensions,
                "checksum": embedding.checksum,
                "_created": False,
            }

        self._ensure_clip_source_asset(media_file, catalog)
        embedding_result = self._materialize_clip_embedding(media_file)
        if embedding_result is None:
            return None

        semantic_catalog.register_embedding(media_file.file_id, **embedding_result)
        clip_rel = Path(embedding_result["embedding_ref"])
        catalog.register_derived_asset(
            media_file.file_id,
            DerivedAssetKind.CLIP_EMBEDDING,
            clip_rel,
            version=embedding_result["version"],
            content_type="application/octet-stream",
        )
        embedding_result["_created"] = True
        return embedding_result

    def _ensure_clip_source_asset(self, media_file: MediaFile, catalog: MediaCatalog) -> None:
        if media_file.media_kind != MediaKind.VIDEO.value:
            return

        thumbnail_path = self._derived_root / self._clip_source_thumbnail_relative_path(media_file.file_id)
        if thumbnail_path.is_file():
            return

        try:
            location = self._thumbnail_service.generate(
                Path(media_file.current_path),
                media_file.file_id,
                MediaKind.VIDEO,
            )
        except Exception as exc:
            logger.warning(
                "video thumbnail unavailable for clip embedding",
                extra={"file_id": media_file.file_id, "path": media_file.current_path, "reason": str(exc)},
            )
            return

        catalog.register_derived_asset(media_file.file_id, location.kind, location.relative_path)

    _GPS_CAPABLE_EXTS = frozenset({".heic", ".heif", ".jpg", ".jpeg", ".tiff", ".tif", ".dng"})

    def _try_repair_gps(self, media_file: MediaFile) -> bool:
        """Re-extract GPS EXIF (and capture datetime) for image files scanned without it.

        Called during semantic maintenance so every library sync automatically
        backfills GPS and exif_datetime for HEIC files scanned before pillow-heif
        was registered.  Returns True if GPS was newly found; also saves
        exif_datetime opportunistically even when GPS is absent.
        """
        if media_file.media_kind != "image":
            return False
        meta = media_file.metadata_json
        already_has_gps = isinstance(meta, dict) and meta.get("gps")
        already_has_datetime = media_file.exif_datetime is not None
        if already_has_gps and already_has_datetime:
            return False
        from pathlib import Path as _Path
        path = _Path(media_file.current_path)
        if path.suffix.lower() not in self._GPS_CAPABLE_EXTS or not path.is_file():
            return False
        try:
            from app.services.image_decode import ensure_heif_support
            from app.services.metadata.service import MetadataService
            from app.core.contracts import FileScanRecord, MediaKind
            ensure_heif_support()
            stat = path.stat()
            scan_record = FileScanRecord(
                path=path,
                source_root=path.parent,
                relative_path=_Path(path.name),
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                media_kind=MediaKind.IMAGE,
            )
            result = MetadataService().extract(scan_record)
            new_extra = result.metadata.extra
            found_gps = isinstance(new_extra, dict) and bool(new_extra.get("gps"))
            found_datetime = not already_has_datetime and result.metadata.captured_at is not None
            if not found_gps and not found_datetime:
                return False
            merged = dict(meta or {})
            if isinstance(new_extra, dict):
                merged.update(new_extra)
            media_file.metadata_json = merged
            if found_datetime:
                media_file.exif_datetime = result.metadata.captured_at
            return found_gps
        except Exception as exc:
            logger.debug("_try_repair_gps failed for %s: %s", media_file.current_path, exc)
            return False

    def _refresh_place_tags(
        self,
        session: Session,
        media_file: MediaFile,
        catalog: MediaCatalog | None = None,
    ) -> list[MediaTagInput]:
        materialized_tags = self._materialize_place_tags(media_file, session=session)
        if not materialized_tags:
            return []

        existing_place_tags = [
            MediaTagInput(tag_type=tag.tag_type, tag_value=tag.tag_value)
            for tag in media_file.tags
            if tag.tag_type in PLACE_TAG_TYPES
        ]
        if _tag_identity_set(existing_place_tags) == _tag_identity_set(materialized_tags):
            return existing_place_tags

        (catalog or MediaCatalog(session)).replace_tags_for_types(
            media_file.file_id,
            list(PLACE_TAG_TYPES),
            materialized_tags,
        )
        return materialized_tags

    def _refresh_auto_tags_from_existing_embedding(self, session: Session, media_file: MediaFile) -> list[MediaTagInput]:
        """Rebuild Phase 2 auto-tags from persisted CLIP vectors when available."""
        if not self._semantic_clip_enabled:
            return []
        embedding = session.execute(
            select(MediaEmbedding)
            .where(
                MediaEmbedding.file_id == media_file.file_id,
                MediaEmbedding.model_name == self._clip_model_identifier(),
                MediaEmbedding.version == self._semantic_embedding_version,
            )
            .order_by(MediaEmbedding.updated_at.desc(), MediaEmbedding.id.desc())
        ).scalars().first()
        if embedding is None:
            SemanticCatalog(session).upsert_auto_tag_state(
                media_file.file_id,
                tags=[],
                version=self._semantic_auto_tag_version,
                source="clip-missing",
            )
            return []

        embedding_tags = auto_tags.tags_from_embedding_file(
            embedding.embedding_ref,
            self._embeddings_root,
        )
        if not embedding_tags:
            SemanticCatalog(session).upsert_auto_tag_state(
                media_file.file_id,
                tags=[],
                version=self._semantic_auto_tag_version,
                source="clip",
            )
            return []

        catalog = MediaCatalog(session)
        preserved_tags, place_tags, person_tags = self._split_existing_tags(media_file.tags)
        preserved_tags = [tag for tag in preserved_tags if tag.tag_type not in auto_tags.AUTO_TAG_TYPES]
        existing_auto_tags = [
            MediaTagInput(tag_type=tag.tag_type, tag_value=tag.tag_value)
            for tag in media_file.tags
            if tag.tag_type in auto_tags.AUTO_TAG_TYPES
        ]
        merged = auto_tags.merge_auto_tags(existing_auto_tags, embedding_tags)
        catalog.replace_tags(media_file.file_id, preserved_tags + place_tags + person_tags + merged)
        SemanticCatalog(session).upsert_auto_tag_state(
            media_file.file_id,
            tags=merged,
            version=self._semantic_auto_tag_version,
            source="clip",
        )
        return merged

    def _analysis_source_path(self, media_file: MediaFile) -> Path:
        """OCR / signal analysis: prefer existing thumbnail (smaller I/O), else source path."""
        thumbnail_path = self._derived_root / self._clip_source_thumbnail_relative_path(media_file.file_id)
        if thumbnail_path.is_file():
            return thumbnail_path
        return Path(media_file.current_path)

    def _materialize_clip_embedding(self, media_file: MediaFile) -> dict[str, Any] | None:
        """CLIP policy A: source image first; videos use derived thumbnails.

        Does not require copying full originals to derived disk. See RUNBOOK «CLIP embedding source policy».
        """
        try:
            clip_embedding.ensure_models()
        except Exception as exc:
            logger.warning(
                "clip embedding skipped",
                extra={"file_id": media_file.file_id, "path": media_file.current_path, "reason": str(exc)},
            )
            return None

        last_error: Exception | None = None
        for source_path in self._clip_embedding_source_paths(media_file):
            try:
                payload = clip_embedding.encode_image(str(source_path))
                break
            except Exception as exc:
                last_error = exc
        else:
            logger.warning(
                "clip embedding skipped",
                extra={
                    "file_id": media_file.file_id,
                    "path": media_file.current_path,
                    "reason": str(last_error) if last_error is not None else "no clip source",
                },
            )
            return None

        vector = clip_embedding.embedding_from_bytes(payload)
        relative_path = self._clip_embedding_relative_path(media_file.file_id)
        absolute_path = self._embedding_absolute_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with NamedTemporaryFile("wb", delete=False, dir=absolute_path.parent) as handle:
                tmp_path = Path(handle.name)
                import numpy as np

                np.save(handle, vector.astype("float32"))
            tmp_path.replace(absolute_path)
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        return {
            "model_name": self._clip_model_identifier(),
            "version": self._semantic_embedding_version,
            "embedding_ref": str(relative_path),
            "dimensions": int(vector.size),
            "checksum": None,
        }

    def _clip_embedding_source_paths(self, media_file: MediaFile) -> list[Path]:
        thumbnail_path = self._derived_root / self._clip_source_thumbnail_relative_path(media_file.file_id)
        if media_file.media_kind == MediaKind.VIDEO.value:
            return [thumbnail_path]

        paths = [Path(media_file.current_path)]
        if thumbnail_path.is_file():
            paths.append(thumbnail_path)
        return paths

    def _clip_model_identifier(self) -> str:
        config = clip_embedding.model_config()
        return f"{config['model_name']}/{config['pretrained']}"

    def _materialize_place_tags(self, media_file: MediaFile, *, session: Session | None = None) -> list[MediaTagInput]:
        metadata = media_file.metadata_json
        if not isinstance(metadata, dict):
            return []

        gps_payload = metadata.get("gps")
        if not isinstance(gps_payload, dict):
            return []

        latitude = _coerce_float(gps_payload.get("latitude"))
        longitude = _coerce_float(gps_payload.get("longitude"))
        if latitude is None or longitude is None:
            return []

        grouped = f"{latitude:.{self._place_tag_precision}f},{longitude:.{self._place_tag_precision}f}"
        detailed = f"{latitude:.7f},{longitude:.7f}"
        tags = [
            MediaTagInput(tag_type="geo", tag_value=grouped),
            MediaTagInput(tag_type="geo_detail", tag_value=detailed),
        ]

        if self._geocoding_enabled and session is not None:
            try:
                geo = CachedGeocodingService(session, self._geocoding_provider, precision=self._place_tag_precision)
                result = geo.reverse(latitude, longitude)
                if result:
                    for place_name in result.place_tags():
                        tags.append(MediaTagInput(tag_type="place", tag_value=place_name))
            except Exception as exc:
                logger.debug("geocoding failed for %s: %s", media_file.file_id, exc)

        return tags

    def repair_missing_gps(self, *, batch_size: int = 200) -> dict[str, Any]:
        """Re-extract EXIF GPS from image files whose stored metadata_json has no gps field.

        Targets image formats that embed GPS (HEIC, JPG, TIFF, etc.) but were
        scanned before pillow-heif was registered.  Skips files that are not
        reachable from the current process (e.g. NAS not mounted in Docker).
        Returns a summary dict with counts.
        """
        from pathlib import Path as _Path
        from app.models.media import MediaFile as _MF
        from app.services.image_decode import ensure_heif_support
        from app.services.metadata.service import MetadataService
        from app.core.contracts import FileScanRecord, MediaKind

        ensure_heif_support()
        metadata_svc = MetadataService()

        # Image extensions that can carry GPS EXIF (videos excluded)
        GPS_CAPABLE = {".heic", ".heif", ".jpg", ".jpeg", ".tiff", ".tif", ".dng"}

        stats: dict[str, int] = {
            "scanned": 0, "skipped_no_file": 0, "skipped_has_gps": 0,
            "gps_found": 0, "gps_not_found": 0, "place_tags_added": 0, "errors": 0,
        }

        with self._session_factory() as session:
            rows = session.execute(
                select(_MF.file_id, _MF.current_path, _MF.media_kind, _MF.metadata_json)
                .where(_MF.status.not_in(("missing", "replaced", "excluded")))
                .where(_MF.media_kind == "image")
            ).all()

        candidates = [
            (fid, path, meta)
            for fid, path, kind, meta in rows
            if _Path(path).suffix.lower() in GPS_CAPABLE
            and not (isinstance(meta, dict) and meta.get("gps"))
        ]

        logger.info("repair_missing_gps: %d candidates (no stored GPS)", len(candidates))

        for fid, path_str, stored_meta in candidates[:batch_size]:
            stats["scanned"] += 1
            path = _Path(path_str)
            if not path.is_file():
                stats["skipped_no_file"] += 1
                continue

            try:
                stat = path.stat()
                scan_record = FileScanRecord(
                    path=path,
                    source_root=path.parent,
                    relative_path=_Path(path.name),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    media_kind=MediaKind.IMAGE,
                )
                result = metadata_svc.extract(scan_record)
                new_extra = result.metadata.extra
                found_gps = isinstance(new_extra, dict) and bool(new_extra.get("gps"))
                found_datetime = result.metadata.captured_at is not None
                if not found_gps and not found_datetime:
                    stats["gps_not_found"] += 1
                    continue

                if found_gps:
                    stats["gps_found"] += 1
                else:
                    stats["gps_not_found"] += 1
                with self._session_factory() as session:
                    media_file = session.get(_MF, fid)
                    if media_file is None:
                        continue
                    merged = dict(stored_meta or {})
                    if isinstance(new_extra, dict):
                        merged.update(new_extra)
                    media_file.metadata_json = merged
                    if media_file.exif_datetime is None and found_datetime:
                        media_file.exif_datetime = result.metadata.captured_at
                    new_tags = self._refresh_place_tags(session, media_file)
                    if new_tags:
                        stats["place_tags_added"] += len(new_tags)
                    session.commit()
            except Exception as exc:
                logger.warning("repair_missing_gps: error on %s: %s", path_str, exc)
                stats["errors"] += 1

        logger.info("repair_missing_gps done: %s", stats)
        return stats

    def repair_missing_exif_datetime(self, batch_size: int = 2000) -> dict[str, int]:
        """Backfill exif_datetime for image files that have none stored.

        Targets any image file (HEIC, JPG, TIFF, DNG, etc.) whose exif_datetime
        is NULL — typically files scanned before pillow-heif support was enabled or
        files where the EXIF sub-IFD tags were not found on first pass.
        Returns a summary dict with counts.
        """
        from pathlib import Path as _Path
        from app.models.media import MediaFile as _MF
        from app.services.image_decode import ensure_heif_support
        from app.services.metadata.service import MetadataService
        from app.core.contracts import FileScanRecord, MediaKind

        ensure_heif_support()
        metadata_svc = MetadataService()
        CAPABLE_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".tiff", ".tif", ".dng", ".png", ".webp"}

        stats: dict[str, int] = {
            "scanned": 0, "skipped_no_file": 0, "datetime_found": 0,
            "datetime_not_found": 0, "errors": 0,
        }

        with self._session_factory() as session:
            rows = session.execute(
                select(_MF.file_id, _MF.current_path, _MF.metadata_json)
                .where(_MF.status.not_in(("missing", "replaced", "excluded")))
                .where(_MF.media_kind == "image")
                .where(_MF.exif_datetime.is_(None))
            ).all()

        candidates = [
            (fid, path, meta)
            for fid, path, meta in rows
            if _Path(path).suffix.lower() in CAPABLE_EXTS
        ]

        logger.info("repair_missing_exif_datetime: %d candidates", len(candidates))

        for fid, path_str, stored_meta in candidates[:batch_size]:
            stats["scanned"] += 1
            path = _Path(path_str)
            if not path.is_file():
                stats["skipped_no_file"] += 1
                continue

            try:
                stat = path.stat()
                scan_record = FileScanRecord(
                    path=path,
                    source_root=path.parent,
                    relative_path=_Path(path.name),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    media_kind=MediaKind.IMAGE,
                )
                result = metadata_svc.extract(scan_record)
                if result.metadata.captured_at is None:
                    stats["datetime_not_found"] += 1
                    continue

                stats["datetime_found"] += 1
                with self._session_factory() as session:
                    media_file = session.get(_MF, fid)
                    if media_file is None:
                        continue
                    media_file.exif_datetime = result.metadata.captured_at
                    new_extra = result.metadata.extra
                    if isinstance(new_extra, dict):
                        merged = dict(stored_meta or {})
                        merged.update(new_extra)
                        media_file.metadata_json = merged
                    session.commit()
            except Exception as exc:
                logger.warning("repair_missing_exif_datetime: error on %s: %s", path_str, exc)
                stats["errors"] += 1

        logger.info("repair_missing_exif_datetime done: %s", stats)
        return stats

    def _refresh_faces_phase2(self, session: Session, media_file: MediaFile, catalog: "MediaCatalog") -> None:
        """Re-run face detection for a single file in Phase 2 and update faces + person tags."""
        if self._face_analysis_service is None:
            # Models not loaded — leave existing face data intact and skip versioning so
            # this file will be retried once models become available.
            return
        face_result = self._materialize_faces(session, media_file)
        if face_result is not None:
            persisted_faces = catalog.upsert_faces(media_file.file_id, face_result.faces)
            catalog.upsert_tags_for_types(media_file.file_id, ["person"], self._person_tags_for_faces(session, persisted_faces))
        else:
            # Analysis failed for this specific file (corrupt image, read error, etc.).
            # Clear stale data so the file is not retried indefinitely.
            catalog.upsert_faces(media_file.file_id, [])
            catalog.upsert_tags_for_types(media_file.file_id, ["person"], [])
        media_file.face_version = self._face_analysis_version

    def _person_tags_for_faces(self, session: Session, faces: list[Face]) -> list[MediaTagInput]:
        person_ids = sorted({int(face.person_id) for face in faces if face.person_id is not None})
        if not person_ids:
            return []
        people = session.scalars(select(Person).where(Person.id.in_(person_ids))).all()
        # display_name뿐 아니라 별칭까지 모두 태깅한다. 이래야 새로 들어온
        # 사진의 person 태그가 일괄 라벨 동기화(_sync_person_search_labels)와
        # 동일해져, "정이한"+별칭("이한") 같은 require_all_persons 검색에서도
        # 최신 사진이 누락되지 않는다. 라벨 산출은 person_labels와 공유한다.
        return [
            MediaTagInput(tag_type="person", tag_value=label)
            for label in person_label_values(people)
        ]

    def _materialize_faces(self, session: Session, media_file: MediaFile) -> FaceMaterializationResult | None:
        if self._face_analysis_service is None:
            return None

        try:
            analysis = self._face_analysis_service.analyze_image_file(Path(media_file.current_path))
        except FaceAnalysisError as exc:
            logger.warning(
                "face analysis skipped",
                extra={"file_id": media_file.file_id, "path": media_file.current_path, "reason": str(exc)},
            )
            return None

        centroid_states = self._load_person_centroids(session)
        face_inputs: list[MediaFaceInput] = []
        person_tags: dict[str, MediaTagInput] = {}
        summaries: list[dict[str, Any]] = []

        for face in analysis.faces:
            person, similarity, is_new_person = self._resolve_person(session, centroid_states, face.embedding)
            embedding_ref = self._write_face_embedding(
                media_file=media_file,
                face_index=face.face_index,
                person=person,
                bbox=_build_bbox_payload(face.bbox),
                embedding=face.embedding,
            )
            face_input = MediaFaceInput(
                bbox=_build_bbox_payload(face.bbox),
                embedding_ref=embedding_ref,
                person_id=person.id,
                person_display_name=person.display_name,
            )
            face_inputs.append(face_input)
            person_tags[person.display_name] = MediaTagInput(tag_type="person", tag_value=person.display_name)
            summaries.append(
                {
                    "face_index": face.face_index,
                    "person_id": person.id,
                    "person": person.display_name,
                    "embedding_ref": embedding_ref,
                    "bbox": face_input.bbox,
                    "match": "new" if is_new_person else "reused",
                    "similarity": round(similarity, 6) if similarity is not None else None,
                }
            )

        return FaceMaterializationResult(
            faces=tuple(face_inputs),
            person_tags=tuple(person_tags.values()),
            summaries=tuple(summaries),
            warnings=analysis.warnings,
        )

    def _load_person_centroids(self, session: Session) -> list[PersonCentroidState]:
        states: list[PersonCentroidState] = []
        # 병합돼 숨겨진 사람은 제외 — 새 얼굴이 숨은 클러스터에 붙으면 안 된다.
        people = session.scalars(
            select(Person).where(Person.merged_into_id.is_(None)).order_by(Person.id.asc())
        ).all()
        for person in people:
            relative_path = self._person_centroid_relative_path(person.id)
            payload = self._read_json(relative_path)
            if not isinstance(payload, dict):
                continue
            centroid = _coerce_embedding(payload.get("embedding"))
            if centroid is None:
                logger.warning("person centroid skipped — invalid embedding", extra={"person_id": person.id})
                continue
            sample_count = _coerce_int(payload.get("sample_count")) or 1
            states.append(
                PersonCentroidState(
                    person=person,
                    centroid=_normalize_embedding(centroid),
                    sample_count=max(1, sample_count),
                    relative_path=relative_path,
                )
            )
        return states

    def _resolve_person(
        self,
        session: Session,
        centroid_states: list[PersonCentroidState],
        embedding: tuple[float, ...],
    ) -> tuple[Person, float | None, bool]:
        normalized_embedding = _normalize_embedding(embedding)
        best_state: PersonCentroidState | None = None
        best_similarity = -1.0

        for state in centroid_states:
            similarity = _cosine_similarity(state.centroid, normalized_embedding)
            if similarity > best_similarity:
                best_similarity = similarity
                best_state = state

        if best_state is not None and best_similarity >= self._face_match_threshold:
            best_state.sample_count += 1
            best_state.centroid = _normalize_embedding(
                tuple(
                    (
                        (best_state.centroid[index] * (best_state.sample_count - 1))
                        + normalized_embedding[index]
                    )
                    / best_state.sample_count
                    for index in range(len(normalized_embedding))
                )
            )
            self._write_person_centroid(best_state)
            return best_state.person, best_similarity, False

        person = self._create_person(session)
        state = PersonCentroidState(
            person=person,
            centroid=normalized_embedding,
            sample_count=1,
            relative_path=self._person_centroid_relative_path(person.id),
        )
        centroid_states.append(state)
        self._write_person_centroid(state)
        return person, None, True

    def _create_person(self, session: Session) -> Person:
        person = Person(display_name=f"person-pending-{uuid4().hex}")
        session.add(person)
        session.flush()
        person.display_name = f"person-{person.id:06d}"
        session.flush()
        return person

    def _write_face_embedding(
        self,
        *,
        media_file: MediaFile,
        face_index: int,
        person: Person,
        bbox: dict[str, Any],
        embedding: tuple[float, ...],
    ) -> str:
        relative_path = self._face_embedding_relative_path(media_file.file_id, face_index)
        self._write_json(
            relative_path,
            {
                "file_id": media_file.file_id,
                "face_index": face_index,
                "person_id": person.id,
                "person": person.display_name,
                "bbox": bbox,
                "embedding": list(embedding),
                "updated_at": datetime.utcnow().isoformat(),
            },
        )
        return str(relative_path)

    def _write_person_centroid(self, state: PersonCentroidState) -> None:
        self._write_json(
            state.relative_path,
            {
                "person_id": state.person.id,
                "person": state.person.display_name,
                "sample_count": state.sample_count,
                "embedding": list(state.centroid),
                "updated_at": datetime.utcnow().isoformat(),
            },
        )

    def _read_json(self, relative_path: Path) -> dict[str, Any] | None:
        absolute_path = self._embedding_absolute_path(relative_path)
        if not absolute_path.is_file():
            return None
        try:
            with absolute_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_json(self, relative_path: Path, payload: dict[str, Any]) -> None:
        absolute_path = self._embedding_absolute_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=absolute_path.parent) as handle:
                tmp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
            tmp_path.replace(absolute_path)
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _face_embedding_relative_path(self, file_id: str, face_index: int) -> Path:
        shard = file_id[:2] if len(file_id) >= 2 else "xx"
        return Path("embeddings") / "faces" / "v1" / shard / f"{file_id}-face-{face_index:03d}.json"

    def _person_centroid_relative_path(self, person_id: int) -> Path:
        return Path("embeddings") / "people" / "v1" / f"person-{person_id:06d}.json"

    def _clip_embedding_relative_path(self, file_id: str) -> Path:
        shard = file_id[:2] if len(file_id) >= 2 else "xx"
        return Path("embeddings") / "clip" / self._semantic_embedding_version / shard / f"{file_id}.npy"

    def _clip_source_thumbnail_relative_path(self, file_id: str) -> Path:
        shard = file_id[:2] if len(file_id) >= 2 else "xx"
        return Path("thumb") / "v1" / shard / f"{file_id}.jpg"

    def _embedding_absolute_path(self, relative_path: Path) -> Path:
        try:
            suffix = relative_path.relative_to("embeddings")
        except ValueError:
            suffix = relative_path
        return self._embeddings_root / suffix

    def _split_existing_tags(
        self,
        tags: list[Tag],
    ) -> tuple[list[MediaTagInput], list[MediaTagInput], list[MediaTagInput]]:
        preserved: list[MediaTagInput] = []
        place_tags: list[MediaTagInput] = []
        person_tags: list[MediaTagInput] = []
        for tag in tags:
            tag_input = MediaTagInput(tag_type=tag.tag_type, tag_value=tag.tag_value)
            if tag.tag_type in PLACE_TAG_TYPES:
                place_tags.append(tag_input)
            elif tag.tag_type in PERSON_TAG_TYPES:
                person_tags.append(tag_input)
            else:
                preserved.append(tag_input)
        return preserved, place_tags, person_tags

    def _job_counts(self, session: Session) -> dict[str, Any]:
        status_rows = session.execute(
            select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)
        ).all()
        kind_rows = session.execute(
            select(ProcessingJob.job_kind, func.count()).group_by(ProcessingJob.job_kind)
        ).all()
        status_counts: dict[str, int] = {}
        kind_counts: dict[str, int] = {}
        for status, count in status_rows:
            status_counts[status] = int(count)
        for job_kind, count in kind_rows:
            kind_counts[job_kind] = int(count)
        return {"status_counts": status_counts, "kind_counts": kind_counts}

    def _active_library_job(self, session: Session) -> dict[str, Any] | None:
        jobs = session.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.job_kind.in_(LIBRARY_JOB_KINDS),
                ProcessingJob.status.in_((ProcessingJobState.QUEUED.value, ProcessingJobState.RUNNING.value)),
            )
            .order_by(ProcessingJob.enqueued_at.asc(), ProcessingJob.updated_at.asc())
        ).scalars().all()
        job = next((item for item in jobs if not self._is_stale_library_job(item)), None)
        if job is None:
            return None
        return {
            "id": job.id,
            "job_kind": job.job_kind,
            "status": job.status,
            "payload": job.payload_json,
            "result": job.result_json,
            "started_at": job.started_at,
            "enqueued_at": job.enqueued_at,
            "updated_at": job.updated_at,
        }

    def _is_stale_library_job(self, job: ProcessingJob) -> bool:
        reference = job.updated_at or job.started_at or job.enqueued_at
        if reference is None:
            return False
        return reference < (datetime.utcnow() - STALE_LIBRARY_JOB_WINDOW)

    def _ensure_no_active_library_job(self, session: Session) -> None:
        active = self._active_library_job(session)
        if active is None:
            return
        raise LibraryJobBusyError(active)

    def _to_summary(self, job: ProcessingJob) -> PipelineSummary:
        return PipelineSummary(
            job_id=job.id,
            job_kind=job.job_kind,
            status=job.status,
            payload=job.payload_json,
            result=job.result_json,
            error_stage=job.error_stage,
            error_message=job.error_message,
        )


def _build_bbox_payload(bbox: Any) -> dict[str, Any]:
    return {
        "x": int(bbox.x),
        "y": int(bbox.y),
        "width": int(bbox.width),
        "height": int(bbox.height),
        "confidence": float(bbox.confidence),
        "landmarks": [[float(x), float(y)] for x, y in bbox.landmarks],
    }


def _coerce_embedding(value: Any) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    values: list[float] = []
    for item in value:
        coerced = _coerce_float(item)
        if coerced is None:
            return None
        values.append(coerced)
    if not values:
        return None
    return tuple(values)


def _normalize_embedding(embedding: tuple[float, ...]) -> tuple[float, ...]:
    magnitude = math.sqrt(sum(value * value for value in embedding))
    if magnitude <= 0.0:
        return embedding
    return tuple(value / magnitude for value in embedding)


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    left_magnitude = math.sqrt(sum(value * value for value in left))
    right_magnitude = math.sqrt(sum(value * value for value in right))
    if left_magnitude <= 0.0 or right_magnitude <= 0.0:
        return -1.0
    return sum(left[index] * right[index] for index in range(len(left))) / (left_magnitude * right_magnitude)


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _tag_identity_set(tags: list[MediaTagInput]) -> set[tuple[str, str]]:
    return {
        (tag.tag_type.strip().lower(), tag.tag_value.strip().casefold())
        for tag in tags
        if tag.tag_type.strip() and tag.tag_value.strip()
    }


def _coerce_source_roots(value: Any) -> tuple[Path, ...] | None:
    if not isinstance(value, list):
        return None
    roots = [Path(str(item)).expanduser().resolve() for item in value if str(item).strip()]
    return tuple(roots) if roots else None


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
LIBRARY_JOB_KINDS = (
    ProcessingJobKind.SCAN.value,
    ProcessingJobKind.SEMANTIC_BACKFILL.value,
    ProcessingJobKind.SEMANTIC_MAINTENANCE.value,
)
STALE_LIBRARY_JOB_WINDOW = timedelta(minutes=5)


class LibraryJobBusyError(RuntimeError):
    def __init__(self, active_job: dict[str, Any]) -> None:
        self.active_job = active_job
        kind = str(active_job.get("job_kind") or "job")
        super().__init__(f"Another library job is active: {kind}")
