"""Application lifespan and startup wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.core.logging import configure_logging
from app.core.settings import AppSettings, load_settings
from app.db.bootstrap import DatabaseState, build_database_state
from app.services.analysis import FaceAnalysisConfig, FaceAnalysisService
from app.services.fingerprint.service import FingerprintConfig, FingerprintService
from app.services.geocoding import NominatimProvider
from app.services.geocoding.local import ChainedGeocodingProvider, LocalGazetteerProvider
from app.services.metadata.service import MetadataService
from app.services.processing.pipeline import ProcessingPipeline
from app.services.search.hybrid import clear_query_cache
from app.services.search.vector import invalidate_global_vector_index
from app.services.scanner.service import DirMtimeCache, ScannerConfig, ScannerService
from app.services.thumbnail.service import ThumbnailConfig, ThumbnailService
from app.services.video.service import VideoKeyframeConfig, VideoKeyframeService
from app.scheduler.service import SchedulerService


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: AppSettings = getattr(app.state, "settings", None) or load_settings()
    configure_logging(settings.log_level)
    if settings.offline_mode:
        import os

        os.environ["PHOTOME_OFFLINE_MODE"] = "1"
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    clear_query_cache()
    invalidate_global_vector_index()
    database = build_database_state(settings)
    scanner = ScannerService(
        ScannerConfig(
            source_roots=settings.source_roots,
            include_hidden_files=settings.include_hidden_files,
            stability_window_seconds=settings.stability_window_seconds,
        )
    )
    fingerprint = FingerprintService(FingerprintConfig(partial_hash_bytes=settings.partial_hash_bytes))
    metadata = MetadataService()
    thumbnail = ThumbnailService(
        ThumbnailConfig(
            derived_root=settings.derived_root,
            size=settings.thumbnail_size,
        )
    )
    keyframes = VideoKeyframeService(
        VideoKeyframeConfig(
            derived_root=settings.derived_root,
        )
    )
    face_analysis = (
        FaceAnalysisService(
            FaceAnalysisConfig(
                model_root=settings.model_root,
                auto_download_models=not settings.offline_mode,
                detection_score_threshold=settings.face_detection_score_threshold,
                min_face_size=settings.face_min_size,
            )
        )
        if settings.face_analysis_enabled
        else None
    )
    if face_analysis is not None:
        try:
            face_analysis.ensure_local_models()
        except Exception as exc:
            logger.warning("face analysis models unavailable", extra={"error": str(exc)})
    local_geocoder = LocalGazetteerProvider(settings.geodata_root)
    geocoding_provider = (
        local_geocoder
        if settings.offline_mode
        else ChainedGeocodingProvider((local_geocoder, NominatimProvider()))
    )
    dir_mtime_cache = DirMtimeCache()
    dir_mtime_cache.attach_persistence(settings.data_root / "scan_cache.json")
    pipeline = ProcessingPipeline(
        database.session_factory,
        scanner,
        fingerprint,
        metadata,
        thumbnail,
        keyframes,
        face_analysis_service=face_analysis,
        derived_root=settings.derived_root,
        embeddings_root=settings.embeddings_root,
        face_match_threshold=settings.face_match_threshold,
        face_analysis_version=settings.face_analysis_version,
        place_tag_precision=settings.place_tag_precision,
        semantic_ocr_enabled=settings.semantic_ocr_enabled,
        semantic_ocr_heuristic_enabled=settings.semantic_ocr_heuristic_enabled,
        semantic_clip_enabled=settings.semantic_clip_enabled,
        semantic_ocr_version=settings.semantic_ocr_version,
        semantic_embedding_version=settings.semantic_embedding_version,
        semantic_auto_tag_version=settings.semantic_auto_tag_version,
        semantic_search_version=settings.semantic_search_version,
        geocoding_provider=geocoding_provider,
        geocoding_enabled=settings.geocoding_enabled,
        asset_processing_workers=settings.asset_processing_workers,
        semantic_maintenance_batch_size=settings.semantic_maintenance_batch_size,
        semantic_manual_batch_size=settings.semantic_manual_batch_size,
        dir_mtime_cache=dir_mtime_cache,
    )
    recovery = pipeline.recover_interrupted_library_jobs()
    scheduler = SchedulerService(settings, pipeline, database.session_factory)

    app.state.settings = settings
    app.state.database = database
    app.state.scanner = scanner
    app.state.fingerprint = fingerprint
    app.state.metadata = metadata
    app.state.thumbnail = thumbnail
    app.state.keyframes = keyframes
    app.state.face_analysis = face_analysis
    app.state.pipeline = pipeline
    app.state.scheduler = scheduler

    scheduler.start()

    logger.info(
        "application startup",
        extra={
            "app_name": settings.app_name,
            "version": settings.app_version,
            "data_root": str(settings.data_root),
            "database_configured": database.configured,
            "recovered_library_jobs": recovery.get("recovered", 0),
        },
    )

    try:
        yield
    finally:
        scheduler.stop()
        logger.info("application shutdown")
