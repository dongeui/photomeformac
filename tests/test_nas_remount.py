from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.services.nas_remount import NasRemounter, parse_network_share_url

MOUNT_OUTPUT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
//user@NAS._smb._tcp.local/homes on /Volumes/homes (smbfs, nodev, nosuid, mounted by user)
//guest@OtherNAS/media on /Volumes/media (smbfs, nodev, nosuid, mounted by user)
"""


def test_parse_network_share_url_matches_mountpoint_and_subpaths() -> None:
    assert (
        parse_network_share_url(MOUNT_OUTPUT, "/Volumes/homes")
        == "smb://user@NAS._smb._tcp.local/homes"
    )
    assert (
        parse_network_share_url(MOUNT_OUTPUT, "/Volumes/homes/user/Photos")
        == "smb://user@NAS._smb._tcp.local/homes"
    )
    assert parse_network_share_url(MOUNT_OUTPUT, "/Volumes/media") == "smb://guest@OtherNAS/media"
    # 로컬 디스크 경로는 매칭되지 않는다.
    assert parse_network_share_url(MOUNT_OUTPUT, "/Users/user/Pictures") is None
    # "/Volumes/homestead"가 "/Volumes/homes"에 잘못 매칭되면 안 된다.
    assert parse_network_share_url(MOUNT_OUTPUT, "/Volumes/homestead") is None


def test_record_and_remount_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remounter = NasRemounter(tmp_path)
    monkeypatch.setattr(remounter, "_read_mounts", lambda: MOUNT_OUTPUT)
    mounted: list[str] = []
    monkeypatch.setattr(remounter, "_mount_volume", lambda url: mounted.append(url) or True)

    remounter.record_url_for_root("/Volumes/homes")
    assert remounter.known_urls() == {"/Volumes/homes": "smb://user@NAS._smb._tcp.local/homes"}

    # 기록은 디스크에 저장돼 재시작(새 인스턴스) 후에도 남는다.
    reloaded = NasRemounter(tmp_path)
    monkeypatch.setattr(reloaded, "_mount_volume", lambda url: mounted.append(url) or True)
    now = datetime.utcnow()
    assert reloaded.try_remount("/Volumes/homes", now) is True
    assert mounted == ["smb://user@NAS._smb._tcp.local/homes"]

    # 스로틀: 10분 내 재시도는 무시, 이후엔 다시 시도.
    assert reloaded.try_remount("/Volumes/homes", now + timedelta(seconds=30)) is False
    assert reloaded.try_remount("/Volumes/homes", now + timedelta(seconds=700)) is True
    assert len(mounted) == 2


def test_try_remount_without_recorded_url_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remounter = NasRemounter(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(remounter, "_mount_volume", lambda url: called.append(url) or True)
    assert remounter.try_remount("/Volumes/unknown", datetime.utcnow()) is False
    assert called == []
