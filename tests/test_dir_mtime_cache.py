from __future__ import annotations

from pathlib import Path

from app.services.scanner.service import DirMtimeCache, ScannerConfig, ScannerService


def test_attach_persistence_loads_existing_entries(tmp_path: Path) -> None:
    cache_path = tmp_path / "scan_cache.json"
    cache_path.write_text('{"/photos/a": 100, "/photos/b": 200}', encoding="utf-8")

    cache = DirMtimeCache()
    cache.attach_persistence(cache_path)

    assert cache.get(Path("/photos/a")) == 100
    assert cache.get(Path("/photos/b")) == 200
    assert len(cache) == 2


def test_save_and_reload_round_trip(tmp_path: Path) -> None:
    cache_path = tmp_path / "subdir" / "scan_cache.json"

    cache = DirMtimeCache()
    cache.attach_persistence(cache_path)
    cache.update(Path("/photos/x"), 42)
    cache.update(Path("/photos/y"), 73)
    cache.save()

    assert cache_path.is_file(), "save() must create the persist file"

    reloaded = DirMtimeCache()
    reloaded.attach_persistence(cache_path)
    assert reloaded.get(Path("/photos/x")) == 42
    assert reloaded.get(Path("/photos/y")) == 73


def test_is_changed_reflects_persisted_state(tmp_path: Path) -> None:
    cache_path = tmp_path / "scan_cache.json"
    cache_path.write_text('{"/photos/x": 500}', encoding="utf-8")

    cache = DirMtimeCache()
    cache.attach_persistence(cache_path)

    # Same mtime → unchanged
    assert cache.is_changed(Path("/photos/x"), 500) is False
    # Different mtime → changed
    assert cache.is_changed(Path("/photos/x"), 501) is True
    # Unknown path → changed (never seen)
    assert cache.is_changed(Path("/photos/z"), 1) is True


def test_save_no_op_without_persist_path(tmp_path: Path) -> None:
    cache = DirMtimeCache()
    cache.update(Path("/a"), 1)
    # No exception even though no attach_persistence
    cache.save()
    # Nothing written
    assert list(tmp_path.iterdir()) == []


def test_corrupt_cache_file_is_ignored(tmp_path: Path) -> None:
    cache_path = tmp_path / "scan_cache.json"
    cache_path.write_text("not json {{{", encoding="utf-8")

    cache = DirMtimeCache()
    cache.attach_persistence(cache_path)
    assert len(cache) == 0  # corrupt file → empty start, no crash


def test_save_entries_writes_snapshot_without_touching_memory(tmp_path: Path) -> None:
    cache_path = tmp_path / "scan_cache.json"
    cache = DirMtimeCache()
    cache.attach_persistence(cache_path)
    cache.update(Path("/photos/a"), 100)
    cache.update(Path("/photos/b"), 200)

    # 체크포인트: 완료된 a만 담긴 스냅샷 저장
    cache.save_entries({"/photos/a": 100})

    reloaded = DirMtimeCache()
    reloaded.attach_persistence(cache_path)
    assert reloaded.get(Path("/photos/a")) == 100
    assert reloaded.get(Path("/photos/b")) is None
    # 메모리 캐시는 그대로
    assert cache.get(Path("/photos/b")) == 200


def test_mark_dirty_forces_reread_but_keeps_seed_entry(tmp_path: Path) -> None:
    cache = DirMtimeCache()
    cache.update(Path("/photos/a"), 100)
    assert not cache.is_changed(Path("/photos/a"), 100)

    cache.mark_dirty(Path("/photos/a"))

    # 반드시 다시 읽히되, walk 시딩 목록(키)에서는 빠지지 않아야 한다 —
    # 키가 빠지면 조상 mtime이 안 변한 중첩 디렉터리는 영영 재방문되지 않는다.
    assert cache.is_changed(Path("/photos/a"), 100)
    assert "/photos/a" in cache.entries()


def _scan_paths(scanner: "ScannerService", root: Path) -> list[str]:
    return sorted(str(record.path.relative_to(root)) for record in scanner.iter_files())


def _make_scanner(root: Path, cache: DirMtimeCache) -> "ScannerService":
    return ScannerService(ScannerConfig(source_roots=(root,)), cache)


def test_walk_finds_new_file_in_nested_dir_with_unchanged_ancestors(tmp_path: Path) -> None:
    """중첩 폴더에만 새 파일이 생기면 조상 디렉터리 mtime은 안 변한다.

    캐시가 생긴 뒤에도 새 파일을 찾아야 한다 — 캐시 키 시딩이 없으면
    walk가 변경 없는 조상에서 서브트리째 스킵해 영영 못 보던 회귀."""
    nested = tmp_path / "2024" / "01"
    nested.mkdir(parents=True)
    (nested / "a.jpg").write_bytes(b"x")

    cache = DirMtimeCache()
    scanner = _make_scanner(tmp_path, cache)
    assert _scan_paths(scanner, tmp_path) == ["2024/01/a.jpg"]

    (nested / "b.jpg").write_bytes(b"y")

    assert "2024/01/b.jpg" in _scan_paths(scanner, tmp_path)


def test_walk_discovers_new_subdirectory_after_cache(tmp_path: Path) -> None:
    nested = tmp_path / "2024"
    nested.mkdir()
    (nested / "a.jpg").write_bytes(b"x")

    cache = DirMtimeCache()
    scanner = _make_scanner(tmp_path, cache)
    assert _scan_paths(scanner, tmp_path) == ["2024/a.jpg"]

    newdir = nested / "02"
    newdir.mkdir()
    (newdir / "c.jpg").write_bytes(b"z")

    assert "2024/02/c.jpg" in _scan_paths(scanner, tmp_path)


def test_has_changes_probe(tmp_path: Path) -> None:
    nested = tmp_path / "2024" / "01"
    nested.mkdir(parents=True)
    (nested / "a.jpg").write_bytes(b"x")

    cache = DirMtimeCache()
    scanner = _make_scanner(tmp_path, cache)

    # 첫 스캔 전(캐시에 루트 없음) → 할 일 있음
    assert scanner.has_changes()

    list(scanner.iter_files())
    assert not scanner.has_changes()

    # 중첩 폴더에만 새 파일 → 조상 mtime 불변이어도 감지
    (nested / "b.jpg").write_bytes(b"y")
    assert scanner.has_changes()

    list(scanner.iter_files())
    assert not scanner.has_changes()

    # dirty 센티널(안정화 대기 디렉터리)도 할 일로 본다
    cache.mark_dirty(nested)
    assert scanner.has_changes()
