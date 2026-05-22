"""Stable file fingerprinting."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from app.core.contracts import FileIdentity, FileScanRecord


@dataclass(frozen=True)
class FingerprintConfig:
    partial_hash_bytes: int = 1_048_576
    fingerprint_version: str = "v1"


class FingerprintService:
    def __init__(self, config: FingerprintConfig | None = None) -> None:
        self._config = config or FingerprintConfig()

    @property
    def config(self) -> FingerprintConfig:
        return self._config

    def fingerprint(self, scan_record: FileScanRecord) -> FileIdentity:
        partial_hash = self._partial_hash(scan_record.path, scan_record.size_bytes)
        file_id = self._compose_file_id(scan_record.size_bytes, scan_record.mtime_ns, partial_hash)
        return FileIdentity(
            file_id=file_id,
            size_bytes=scan_record.size_bytes,
            mtime_ns=scan_record.mtime_ns,
            partial_hash=partial_hash,
            fingerprint_version=self._config.fingerprint_version,
            media_kind=scan_record.media_kind,
        )

    def _partial_hash(self, path: Path, size_bytes: int) -> str:
        sample_bytes = max(1, self._config.partial_hash_bytes)
        hasher = hashlib.sha256()
        hasher.update(str(size_bytes).encode("utf-8"))

        with path.open("rb") as stream:
            if size_bytes <= sample_bytes * 2:
                hasher.update(stream.read())
            else:
                head = stream.read(sample_bytes)
                hasher.update(head)
                tail_offset = max(0, size_bytes - sample_bytes)
                stream.seek(tail_offset)
                hasher.update(stream.read(sample_bytes))

        return hasher.hexdigest()

    def _compose_file_id(self, size_bytes: int, mtime_ns: int, partial_hash: str) -> str:
        digest = hashlib.sha256()
        digest.update(str(size_bytes).encode("utf-8"))
        digest.update(b":")
        digest.update(str(mtime_ns).encode("utf-8"))
        digest.update(b":")
        digest.update(partial_hash.encode("utf-8"))
        return digest.hexdigest()
