"""Incremental scan reconciliation logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import time
from typing import Any, Callable
import unicodedata

from sqlalchemy.orm import Session

from app.core.contracts import FileScanRecord, MediaKind, media_kind_from_path
from app.services.fingerprint.service import FingerprintService
from app.services.metadata.service import MetadataService
from app.services.processing.registry import MediaCatalog
from app.services.scanner.service import _DIRTY_MTIME_SENTINEL, DirMtimeCache, ScannerService, _path_exists


logger = logging.getLogger(__name__)

# 스캔 도중 dir mtime 캐시 체크포인트 저장 주기. 수만 장 NAS 라이브러리의
# 첫 검증 walk는 몇 시간씩 걸리는데, 완주 시점에만 저장하면 중간에 재시작/
# NAS 끊김이 한 번이라도 나면 다음 스캔이 전체를 처음부터 다시 걷는다.
_SCAN_CACHE_CHECKPOINT_SECONDS = 60.0


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
        # `or` 금지: DirMtimeCache는 __len__이 있어 비어 있으면 falsy다.
        self._dir_mtime_cache = dir_mtime_cache if dir_mtime_cache is not None else DirMtimeCache()

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

        # missing으로 표시된 파일이 디렉터리 변경 없이 되살아난 경우(NAS 재연결,
        # 수동 복원 등)는 mtime 캐시 때문에 walk가 못 본다. 따로 재확인한다.
        # seen_paths는 DB 사전 채움이 섞여 있어 기준이 못 되고, walk가 실제로
        # 내놓은 경로만 중복 제외한다.
        walked_paths = {unicodedata.normalize("NFC", str(record.path)) for record in scan_records}
        for missing_root, missing_path in catalog.iter_missing_media_paths(active_source_roots):
            nfc_path = unicodedata.normalize("NFC", missing_path)
            if nfc_path in walked_paths:
                continue
            path = Path(missing_path)
            media_kind = media_kind_from_path(path)
            if media_kind != MediaKind.IMAGE:
                continue
            try:
                stat_result = path.stat()
            except OSError:
                continue
            root = Path(missing_root)
            try:
                relative_path = path.relative_to(root)
            except ValueError:
                continue
            seen_paths.add(nfc_path)
            scan_records.append(
                FileScanRecord(
                    source_root=root,
                    path=path,
                    relative_path=relative_path,
                    size_bytes=stat_result.st_size,
                    mtime_ns=stat_result.st_mtime_ns,
                    media_kind=media_kind,
                )
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

        # 체크포인트: walk가 캐시에 미리 적어둔 디렉터리 중 "모든 파일의 DB
        # 반영이 끝난" 디렉터리까지만 디스크에 저장한다. walk는 디렉터리
        # 단위로 파일을 연속으로 내놓으므로, 부모 경로가 바뀌는 순간 직전
        # 디렉터리는 완료된 것이다. 안정화 대기 파일이 있는 디렉터리는
        # 캐시에 남기면 그 파일이 영영 다시 관찰되지 않으므로 제외한다.
        checkpoint_entries = self._dir_mtime_cache.entries()
        unstable_dirs: set[str] = set()
        previous_parent: str | None = None
        last_checkpoint_at = time.monotonic()

        def _fold_completed_dir(dir_key: str) -> None:
            if dir_key in unstable_dirs:
                # 키를 빼지 않고 dirty 센티널로 남긴다 — walk 시딩 목록에는
                # 있어야 중단 후 재시작해도 이 디렉터리가 다시 읽힌다.
                checkpoint_entries[dir_key] = _DIRTY_MTIME_SENTINEL
                return
            cached_mtime = self._dir_mtime_cache.get(Path(dir_key))
            if cached_mtime is not None:
                checkpoint_entries[dir_key] = cached_mtime

        for index, scan_record in enumerate(scan_records, start=1):
            scanned = index
            parent_key = str(scan_record.path.parent)
            if previous_parent is not None and parent_key != previous_parent:
                _fold_completed_dir(previous_parent)
                checkpoint_now = time.monotonic()
                if checkpoint_now - last_checkpoint_at >= _SCAN_CACHE_CHECKPOINT_SECONDS:
                    # 캐시보다 DB가 앞서야 안전하다(반대면 사진이 유실된다).
                    session.commit()
                    self._dir_mtime_cache.save_entries(checkpoint_entries)
                    last_checkpoint_at = checkpoint_now
            previous_parent = parent_key

            observation = catalog.observe_scan(
                scan_record,
                now=now,
                stability_window_seconds=self._scanner.config.stability_window_seconds,
            )
            if not observation.ready:
                unstable_dirs.add(parent_key)
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
        # 안정화 대기 파일이 있던 디렉터리는 dirty 표시해 다음 스캔이 반드시
        # 다시 읽게 한다(키를 지우면 walk 시딩에서 빠져 영영 재방문 안 됨).
        for dir_key in unstable_dirs:
            self._dir_mtime_cache.mark_dirty(dir_key)
        self._dir_mtime_cache.save()
        return IncrementalScanSummary(
            scanned=scanned,
            created=created,
            updated=updated,
            moved=moved,
            missing=missing,
            failed=failed,
        )
