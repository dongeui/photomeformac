"""Filesystem scanner for source roots."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import errno
import logging
import os
from pathlib import Path
import time
from typing import Iterator

from app.core.contracts import FileScanRecord, MediaKind, media_kind_from_path


logger = logging.getLogger(__name__)

_TRANSIENT_ERRNOS = {
    errno.EAGAIN,
    errno.EWOULDBLOCK,
    errno.ETIMEDOUT,
    errno.ECONNRESET,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
}
_TRANSIENT_RETRIES = 3
_TRANSIENT_RETRY_DELAY_SECONDS = 1.0


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError as exc:
        logger.warning("source path unavailable", extra={"path": str(path), "error": str(exc)})
        return False


@dataclass(frozen=True)
class ScannerConfig:
    source_roots: tuple[Path, ...]
    include_hidden_files: bool = False
    follow_symlinks: bool = False
    stability_window_seconds: int = 300


@dataclass
class DirMtimeCache:
    """Per-source-root directory mtime cache for delta scanning.

    Maps absolute directory path → mtime_ns from the last completed scan.
    When a directory's mtime is unchanged the caller can skip re-walking it
    and use the previously-recorded file list instead.
    """
    _mtimes: dict[str, int] = field(default_factory=dict)

    def is_changed(self, dir_path: Path, current_mtime_ns: int) -> bool:
        key = str(dir_path)
        return self._mtimes.get(key) != current_mtime_ns

    def update(self, dir_path: Path, mtime_ns: int) -> None:
        self._mtimes[str(dir_path)] = mtime_ns

    def get(self, dir_path: Path) -> int | None:
        return self._mtimes.get(str(dir_path))

    def clear(self) -> None:
        self._mtimes.clear()


class ScannerService:
    def __init__(self, config: ScannerConfig, dir_mtime_cache: DirMtimeCache | None = None) -> None:
        self._config = config
        self._error_count = 0
        self._dir_mtime_cache = dir_mtime_cache

    @property
    def config(self) -> ScannerConfig:
        return self._config

    def with_source_roots(self, source_roots: tuple[Path, ...]) -> "ScannerService":
        return ScannerService(replace(self._config, source_roots=source_roots), self._dir_mtime_cache)

    @property
    def error_count(self) -> int:
        return self._error_count

    def iter_files(self) -> Iterator[FileScanRecord]:
        for source_root in self._config.source_roots:
            yield from self._iter_root(source_root)

    def _iter_root(self, source_root: Path) -> Iterator[FileScanRecord]:
        if not _path_exists(source_root):
            logger.warning("source root missing", extra={"source_root": str(source_root)})
            return

        for current_path in self._walk(source_root):
            media_kind = media_kind_from_path(current_path)
            if media_kind != MediaKind.IMAGE:
                continue
            try:
                stat_result = self._stat(current_path)
            except OSError as exc:
                self._error_count += 1
                logger.warning(
                    "unable to stat file",
                    extra={"path": str(current_path), "error": str(exc)},
                )
                continue

            relative_path = current_path.relative_to(source_root)
            yield FileScanRecord(
                source_root=source_root,
                path=current_path,
                relative_path=relative_path,
                size_bytes=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
                media_kind=media_kind,
            )

    def _walk(self, source_root: Path) -> Iterator[Path]:
        """Walk source_root yielding file paths.

        When a DirMtimeCache is attached, directories whose mtime_ns matches
        the cached value are skipped — their known-good paths are supplied by
        the caller via _cached_paths (populated by IncrementalScanService).
        Only directories whose mtime changed (new/deleted/renamed files) are
        re-read from the filesystem.
        """
        cache = self._dir_mtime_cache
        stack = [source_root]
        while stack:
            current_dir = stack.pop()

            # --- delta shortcut: skip unchanged directories ---
            if cache is not None:
                try:
                    dir_stat = os.stat(current_dir)
                    current_mtime_ns = dir_stat.st_mtime_ns
                except OSError:
                    current_mtime_ns = None

                if current_mtime_ns is not None and not cache.is_changed(current_dir, current_mtime_ns):
                    # Directory unchanged → no new/deleted files here.
                    # Yield nothing; seen_paths will be populated from DB records.
                    continue

            try:
                entries = self._scandir(current_dir)
            except OSError as exc:
                self._error_count += 1
                logger.warning(
                    "unable to read directory",
                    extra={"path": str(current_dir), "error": str(exc)},
                )
                continue

            # Update cache after successful read
            if cache is not None and current_mtime_ns is not None:
                cache.update(current_dir, current_mtime_ns)

            for entry in sorted(entries, key=lambda item: item.name):
                if not self._config.include_hidden_files and entry.name.startswith("."):
                    continue

                path = Path(entry.path)
                try:
                    if entry.is_symlink() and not self._config.follow_symlinks:
                        continue
                    if entry.is_dir(follow_symlinks=self._config.follow_symlinks):
                        stack.append(path)
                        continue
                    if entry.is_file(follow_symlinks=self._config.follow_symlinks):
                        yield path
                except OSError as exc:
                    self._error_count += 1
                    logger.warning(
                        "unable to inspect entry",
                        extra={"path": str(path), "error": str(exc)},
                    )

    def _scandir(self, path: Path) -> list[os.DirEntry[str]]:
        def action() -> list[os.DirEntry[str]]:
            with os.scandir(path) as iterator:
                return list(iterator)

        return self._retry_transient_os_error(action)

    def _stat(self, path: Path) -> os.stat_result:
        return self._retry_transient_os_error(
            lambda: os.stat(path, follow_symlinks=self._config.follow_symlinks)
        )

    def _retry_transient_os_error(self, action):
        last_error: OSError | None = None
        for attempt in range(_TRANSIENT_RETRIES + 1):
            try:
                return action()
            except OSError as exc:
                if exc.errno not in _TRANSIENT_ERRNOS or attempt >= _TRANSIENT_RETRIES:
                    raise
                last_error = exc
                time.sleep(_TRANSIENT_RETRY_DELAY_SECONDS * (attempt + 1))
        raise last_error or RuntimeError("unreachable transient retry state")
