"""Filesystem scanner for source roots."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import errno
import json
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

# mark_dirty()가 쓰는 mtime 센티널. 실제 mtime_ns(>=0)와 절대 일치하지 않아
# is_changed()가 항상 True → 다음 스캔이 반드시 다시 읽는다.
_DIRTY_MTIME_SENTINEL = -1


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

    Persisted to JSON on disk so that the next backend boot can skip rewalking
    unchanged directories — without this, every fresh process treats the whole
    library as if it were never scanned.
    """
    _mtimes: dict[str, int] = field(default_factory=dict)
    _persist_path: Path | None = None

    def is_changed(self, dir_path: Path, current_mtime_ns: int) -> bool:
        key = str(dir_path)
        return self._mtimes.get(key) != current_mtime_ns

    def update(self, dir_path: Path, mtime_ns: int) -> None:
        self._mtimes[str(dir_path)] = mtime_ns

    def get(self, dir_path: Path) -> int | None:
        return self._mtimes.get(str(dir_path))

    def entries(self) -> dict[str, int]:
        return dict(self._mtimes)

    def mark_dirty(self, dir_path: Path | str) -> None:
        """다음 스캔이 이 디렉터리를 반드시 다시 읽도록 표시한다.

        키를 지우지 않고 센티널로 덮는다 — walk가 캐시 키를 시작점(시딩)으로
        삼기 때문에, 키를 지우면 조상 mtime이 안 변한 중첩 디렉터리는 영영
        다시 방문되지 않는다.
        """
        self._mtimes[str(dir_path)] = _DIRTY_MTIME_SENTINEL

    def clear(self) -> None:
        self._mtimes.clear()

    def __len__(self) -> int:
        return len(self._mtimes)

    def attach_persistence(self, path: Path) -> None:
        """Bind this cache to a JSON file. Loads existing entries if present."""
        self._persist_path = path
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("scan cache load failed", extra={"path": str(path), "error": str(exc)})
            return
        if not isinstance(payload, dict):
            return
        loaded: dict[str, int] = {}
        for key, value in payload.items():
            try:
                loaded[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        self._mtimes = loaded

    def save(self) -> None:
        self.save_entries(self._mtimes)

    def save_entries(self, entries: dict[str, int]) -> None:
        """임의 스냅샷을 원자적으로 저장한다.

        스캔 도중 체크포인트 저장용 — 메모리의 전체 캐시(_mtimes)에는 아직
        DB에 ingest되지 않은 디렉터리도 들어 있어, '완전히 처리된 디렉터리
        까지'만 담은 스냅샷을 따로 받아 저장해야 중단돼도 안전하다.
        """
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self._persist_path)
        except OSError as exc:
            logger.warning("scan cache save failed", extra={"path": str(self._persist_path), "error": str(exc)})


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

    def _seed_dirs(self, source_root: Path) -> list[Path]:
        """walk 시작점: 소스 루트 + 캐시에 기록된 그 하위 디렉터리 전부.

        변경 없는 디렉터리는 스킵되면서 하위로도 내려가지 않는다. POSIX에서
        중첩 폴더에 파일이 생겨도 조상 디렉터리 mtime은 안 바뀌므로, 루트만
        시작점으로 삼으면 캐시가 생긴 뒤 중첩 폴더의 새 파일을 영영 못 본다.
        캐시에 있는 모든 디렉터리를 직접 시작점으로 삼아 각자 stat으로
        변화를 판정한다.
        """
        dirs = [source_root]
        cache = self._dir_mtime_cache
        if cache is not None:
            root_key = str(source_root)
            prefix = root_key + os.sep
            for key in cache.entries():
                if key != root_key and key.startswith(prefix):
                    dirs.append(Path(key))
        return dirs

    def has_changes(self) -> bool:
        """스캔 없이 '동기화할 파일 변화가 있는지'만 stat 스윕으로 판정한다.

        스케줄러가 유휴 틱마다 호출하는 저비용 프로브. 캐시에 없는 소스
        루트(첫 스캔 전), mtime이 달라진 디렉터리(파일 추가/삭제, 새 하위
        폴더 생성, dirty 센티널)가 하나라도 있으면 True.
        """
        cache = self._dir_mtime_cache
        if cache is None:
            return True
        for source_root in self._config.source_roots:
            # 미마운트 NAS는 변화로 치지 않는다 — 재연결 시점에
            # nas-reconnect 스캔이 따로 제출된다.
            if not _path_exists(source_root):
                continue
            for dir_path in self._seed_dirs(source_root):
                try:
                    mtime_ns = os.stat(dir_path).st_mtime_ns
                except OSError:
                    # 삭제된 디렉터리는 부모 mtime 변화로 함께 감지된다.
                    continue
                if cache.is_changed(dir_path, mtime_ns):
                    return True
        return False

    def _walk(self, source_root: Path) -> Iterator[Path]:
        """Walk source_root yielding file paths.

        When a DirMtimeCache is attached, directories whose mtime_ns matches
        the cached value are skipped — their known-good paths are supplied by
        the caller via _cached_paths (populated by IncrementalScanService).
        Only directories whose mtime changed (new/deleted/renamed files) are
        re-read from the filesystem. 캐시에 기록된 하위 디렉터리는 전부
        시작점으로 시딩되므로(_seed_dirs) 조상이 안 변해도 각자 판정된다.
        """
        cache = self._dir_mtime_cache
        stack = self._seed_dirs(source_root)
        visited: set[str] = set()
        while stack:
            current_dir = stack.pop()
            current_key = str(current_dir)
            if current_key in visited:
                continue
            visited.add(current_key)

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
