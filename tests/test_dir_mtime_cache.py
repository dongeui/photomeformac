from __future__ import annotations

from pathlib import Path

from app.services.scanner.service import DirMtimeCache


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


def test_forget_removes_entry_so_dir_is_rewalked(tmp_path: Path) -> None:
    cache = DirMtimeCache()
    cache.update(Path("/photos/a"), 100)
    assert not cache.is_changed(Path("/photos/a"), 100)

    cache.forget(Path("/photos/a"))

    assert cache.is_changed(Path("/photos/a"), 100)
    assert cache.entries() == {}
