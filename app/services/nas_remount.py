"""Automatic SMB/AFP remount for NAS source roots (macOS).

macOS는 네트워크 볼륨이 절전/네트워크 끊김으로 내려가면 사용자가 Finder에서
서버를 한 번 열어줘야 다시 붙는다. 백엔드가 이를 대신한다:

- 소스 루트가 살아있는 동안 `mount` 출력에서 해당 볼륨의 smb/afp URL을 캡처해
  data_root에 저장한다(설정 불필요, 자격증명은 macOS Keychain의 것을 그대로 사용).
- 루트가 unreachable로 바뀌면 기록된 URL로 `osascript -e 'mount volume ...'`을
  주기적으로(스로틀) 시도한다. 성공하면 기존 스케줄러의 reconnect 경로가
  증분 스캔을 자동으로 올린다.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REMOUNT_URLS_FILENAME = "nas-remount-urls.json"
_REMOUNT_MIN_INTERVAL_SECONDS = 600.0

# `mount` 출력 예: "//user@NAS._smb._tcp.local/homes on /Volumes/homes (smbfs, ...)"
_MOUNT_LINE_RE = re.compile(r"^//(?P<spec>\S+) on (?P<mountpoint>/\S+) \((?P<fstype>[a-z]+)")


def parse_network_share_url(mount_output: str, source_root: str) -> str | None:
    """Return the smb:// or afp:// URL of the network mount containing source_root."""
    best: tuple[int, str] | None = None
    for line in mount_output.splitlines():
        match = _MOUNT_LINE_RE.match(line.strip())
        if match is None:
            continue
        fstype = match.group("fstype")
        if fstype not in ("smbfs", "afpfs"):
            continue
        mountpoint = match.group("mountpoint")
        if source_root != mountpoint and not source_root.startswith(mountpoint.rstrip("/") + "/"):
            continue
        scheme = "smb" if fstype == "smbfs" else "afp"
        url = f"{scheme}://{match.group('spec')}"
        # 더 깊은(구체적인) 마운트포인트를 우선한다.
        candidate = (len(mountpoint), url)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


class NasRemounter:
    """Capture network-share URLs while mounted; remount them when they drop."""

    def __init__(self, data_root: Path) -> None:
        self._store_path = Path(data_root) / REMOUNT_URLS_FILENAME
        self._urls: dict[str, str] = self._load()
        self._last_attempt_at: dict[str, datetime] = {}

    def record_url_for_root(self, source_root: str, mount_output: str | None = None) -> None:
        """소스 루트가 살아있을 때 호출 — 네트워크 마운트면 URL을 기록한다."""
        try:
            if source_root in self._urls:
                return
            if mount_output is None:
                mount_output = self._read_mounts()
            url = parse_network_share_url(mount_output, source_root)
            if not url or '"' in url:
                return
            self._urls[source_root] = url
            self._save()
            logger.info(
                "recorded NAS remount url", extra={"source_root": source_root, "url": url}
            )
        except Exception as exc:
            logger.debug("nas remount url record failed: %s", exc)

    def try_remount(self, source_root: str, now: datetime) -> bool:
        """unreachable 소스 루트의 재마운트를 시도한다(스로틀 적용)."""
        url = self._urls.get(source_root)
        if not url:
            return False
        last = self._last_attempt_at.get(source_root)
        if last is not None and (now - last).total_seconds() < _REMOUNT_MIN_INTERVAL_SECONDS:
            return False
        self._last_attempt_at[source_root] = now
        ok = self._mount_volume(url)
        if ok:
            logger.info("NAS remount attempted", extra={"source_root": source_root, "url": url})
        else:
            logger.warning(
                "NAS remount failed", extra={"source_root": source_root, "url": url}
            )
        return ok

    def known_urls(self) -> dict[str, str]:
        return dict(self._urls)

    def _mount_volume(self, url: str) -> bool:
        try:
            result = subprocess.run(
                ["osascript", "-e", f'mount volume "{url}"'],
                capture_output=True,
                text=True,
                timeout=45,
            )
        except Exception as exc:
            logger.debug("osascript mount failed: %s", exc)
            return False
        return result.returncode == 0

    def _read_mounts(self) -> str:
        result = subprocess.run(["mount"], capture_output=True, text=True, timeout=10)
        return result.stdout

    def _load(self) -> dict[str, str]:
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items() if isinstance(v, str)}

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(
                json.dumps(self._urls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("nas remount url save failed: %s", exc)
