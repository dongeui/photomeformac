"""Incremental scan reconciliation logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Callable
import unicodedata

from sqlalchemy.orm import Session

from app.services.fingerprint.service import FingerprintService
from app.services.metadata.service import MetadataService
from app.services.processing.registry import MediaCatalog
from app.services.scanner.service import DirMtimeCache, ScannerService, _path_exists


logger = logging.getLogger(__name__)


def _scan_progress_message(total: int, current: int, created: int, updated: int) -> str:
    """Human-friendly Korean copy that distinguishes 'checked' vs 'actually changed'.

    The pipeline iterates every scan record (potentially every photo seen on the
    NAS) just to compare fingerprints, so the legacy "{total}개 처리 중" message
    misled users into thinking the whole library was being reprocessed.
    """
    if total == 0:
        return "변경된 사진이 없습니다."
    changed = created + updated
    if changed:
        return (
            f"{total}장 검사 중 ({current}/{total}) · "
            f"신규 {created}장, 갱신 {updated}장 처리 중"
        )
    return f"{total}장 검사 중 ({current}/{total}) · 변경 없음"


@dataclass(frozen=True)
class IncrementalScanSummary:
    scanned: int = 0
    created: int = 0
    updated: int = 0
    moved: int = 0
    missing: int = 0
    failed: int = 0


class IncrementalScanService:
    def __init__(
        self,
        scanner: ScannerService,
        fingerprint_service: FingerprintService,
        metadata_service: MetadataService,
        *,
        dir_mtime_cache: DirMtimeCache | None = None,
    ) -> None:
        self._scanner = scanner
        self._fingerprint_service = fingerprint_service
        self._metadata_service = metadata_service
        # Persists across scan invocations (and across backend restarts when
        # attach_persistence has been set on the cache) to enable delta scanning.
        self._dir_mtime_cache = dir_mtime_cache or DirMtimeCache()

    def run(self, session: Session, progress_callback: Callable[[dict[str, Any]], None] | None = None) -> IncrementalScanSummary:
        catalog = MediaCatalog(session)
        scanned = created = updated = moved = failed = 0
        seen_paths: set[str] = set()
        source_roots = tuple(self._scanner.config.source_roots)
        active_source_roots = {
            str(source_root)
            for source_root in source_roots
            if _path_exists(source_root)
        }
        now = datetime.utcnow()

        # Attach the persistent dir-mtime cache so the scanner can skip
        # directories whose mtime hasn't changed since the last scan.
        delta_scanner = ScannerService(self._scanner.config, self._dir_mtime_cache)

        # Pre-populate seen_paths only for DB files inside directories whose
        # mtimes are unchanged. Changed directories are freshly walked below, so
        # deleted files there must not be marked as seen from the stale DB path.
        for db_path in catalog.iter_known_paths(active_source_roots):
            parent = Path(db_path).parent
            try:
                parent_mtime_ns = parent.stat().st_mtime_ns
            except OSError:
                continue
            if not self._dir_mtime_cache.is_changed(parent, parent_mtime_ns):
                seen_paths.add(unicodedata.normalize("NFC", db_path))

        scan_records = []
        total_roots = len(source_roots)
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "discovering_files",
                    "message": "동기화 경로를 확인하고 있습니다.",
                    "files_found": 0,
                    "source_root_total": total_roots,
                }
            )
        for root_index, source_root in enumerate(source_roots, start=1):
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "discovering_files",
                        "message": f"경로를 읽고 있습니다: {source_root}",
                        "source_root": str(source_root),
                        "source_root_index": root_index,
                        "source_root_total": total_roots,
                        "files_found": len(scan_records),
                    }
                )
            root_scanner = delta_scanner.with_source_roots((source_root,))
            for scan_record in root_scanner.iter_files():
                # Freshy-walked files always override the pre-populated DB entry
                nfc_path = unicodedata.normalize("NFC", str(scan_record.path))
                seen_paths.add(nfc_path)
                scan_records.append(scan_record)
                if progress_callback is not None and len(scan_records) % 100 == 0:
                    progress_callback(
                        {
                            "stage": "discovering_files",
                            "message": f"{len(scan_records)}개의 파일을 찾았습니다.",
                            "source_root": str(source_root),
                            "source_root_index": root_index,
                            "source_root_total": total_roots,
                            "files_found": len(scan_records),
                        }
                    )
            if root_scanner.error_count:
                failed += root_scanner.error_count
                logger.warning(
                    "scan root had filesystem read errors; missing reconciliation will be skipped",
                    extra={"source_root": str(source_root), "error_count": root_scanner.error_count},
                )

        total = len(scan_records)
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "processing_scan_records",
                    "message": _scan_progress_message(total, 0, 0, 0),
                    "files_found": total,
                    "scan": {"current": 0, "total": total, "succeeded": 0, "failed": 0},
                }
            )

        for index, scan_record in enumerate(scan_records, start=1):
            scanned = index
            observation = catalog.observe_scan(
                scan_record,
                now=now,
                stability_window_seconds=self._scanner.config.stability_window_seconds,
            )
            if not observation.ready:
                continue

            try:
                identity = self._fingerprint_service.fingerprint(scan_record)
            except Exception as exc:
                failed += 1
                catalog.mark_observation_error(
                    scan_record,
                    stage="fingerprint",
                    message=str(exc),
                    now=now,
                    stability_window_seconds=self._scanner.config.stability_window_seconds,
                )
                logger.exception("failed to fingerprint scanned file", extra={"path": str(scan_record.path)})
                if progress_callback is not None and (index == 1 or index == total or index % 25 == 0):
                    progress_callback(
                        {
                            "stage": "processing_scan_records",
                            "message": _scan_progress_message(total, index, created, updated),
                            "files_found": total,
                            "current_path": str(scan_record.path),
                            "scan": {
                                "current": index,
                                "total": total,
                                "succeeded": max(0, index - failed),
                                "failed": failed,
                            },
                        }
                    )
                continue

            metadata_result = None
            metadata_error: str | None = None
            try:
                metadata_result = self._metadata_service.extract(scan_record)
            except Exception as exc:
                metadata_error = str(exc)

            change = catalog.upsert_media_file(
                scan_record,
                identity,
                metadata_result.metadata if metadata_result is not None else None,
                now=now,
            )
            if change.action == "created":
                created += 1
            elif change.action == "moved":
                moved += 1
                updated += 1
            elif change.action == "replaced":
                updated += 1
            elif change.action == "updated":
                updated += 1

            if metadata_error is not None:
                failed += 1
                catalog.mark_media_error(identity.file_id, stage="metadata", message=metadata_error, now=now)
                logger.error("failed to extract metadata for scanned file", extra={"path": str(scan_record.path)})
            elif metadata_result is not None and metadata_result.warnings:
                logger.debug(
                    "metadata warnings",
                    extra={"path": str(scan_record.path), "warnings": metadata_result.warnings},
                )

            if progress_callback is not None and (index == 1 or index == total or index % 25 == 0):
                progress_callback(
                    {
                        "stage": "processing_scan_records",
                        "message": _scan_progress_message(total, index, created, updated),
                        "files_found": total,
                        "current_path": str(scan_record.path),
                        "scan": {
                            "current": index,
                            "total": total,
                            "succeeded": max(0, index - failed),
                            "failed": failed,
                        },
                    }
                )

        if self._scanner.config.source_roots and not active_source_roots:
            logger.warning("all source roots unavailable; skipping missing reconciliation")
            missing = 0
        elif failed > 0:
            logger.warning(
                "scan had %d failures; skipping missing reconciliation to avoid false-missing",
                failed,
            )
            missing = 0
        elif scanned < 100:
            logger.warning(
                "scan yielded fewer than 100 files (%d); skipping missing reconciliation to avoid false-missing",
                scanned,
            )
            missing = 0
        else:
            missing = catalog.mark_missing_except(seen_paths, active_source_roots)
        session.commit()
        # Persist directory mtime cache so the next backend boot skips unchanged
        # directories instead of treating the whole library as new.
        self._dir_mtime_cache.save()
        return IncrementalScanSummary(
            scanned=scanned,
            created=created,
            updated=updated,
            moved=moved,
            missing=missing,
            failed=failed,
        )
