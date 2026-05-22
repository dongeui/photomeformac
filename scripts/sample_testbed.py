#!/usr/bin/env python3
"""Copy a bounded local testbed from source roots without path-based duplication."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.fingerprint.service import FingerprintService
from app.services.scanner.service import ScannerConfig, ScannerService


@dataclass(frozen=True)
class SampleResult:
    source_root: str
    copied: int
    skipped_existing: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", action="append", required=True, dest="source_roots")
    parser.add_argument("--dest-root", required=True)
    parser.add_argument("--limit-per-root", type=int, default=50)
    parser.add_argument("--manifest-path", default="")
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "items": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    args = parse_args()
    source_roots = tuple(Path(value).expanduser().resolve() for value in args.source_roots)
    dest_root = Path(args.dest_root).expanduser().resolve()
    manifest_path = (
        Path(args.manifest_path).expanduser().resolve()
        if args.manifest_path
        else dest_root / ".sample_manifest.json"
    )

    scanner = ScannerService(ScannerConfig(source_roots=source_roots))
    fingerprint = FingerprintService()
    manifest = load_manifest(manifest_path)
    items = manifest.setdefault("items", {})

    per_root_copied: dict[str, int] = {str(root): 0 for root in source_roots}
    per_root_skipped: dict[str, int] = {str(root): 0 for root in source_roots}

    for scan_record in scanner.iter_files():
        source_key = str(scan_record.source_root)
        if per_root_copied[source_key] >= args.limit_per_root:
            continue

        identity = fingerprint.fingerprint(scan_record)
        existing = items.get(identity.file_id)
        if existing is not None:
            per_root_skipped[source_key] += 1
            existing["last_observed_path"] = str(scan_record.path)
            existing["last_observed_mtime_ns"] = scan_record.mtime_ns
            existing["last_observed_size_bytes"] = scan_record.size_bytes
            continue

        shard = identity.file_id[:2]
        output_dir = dest_root / shard
        output_dir.mkdir(parents=True, exist_ok=True)
        output_name = f"{identity.file_id}__{scan_record.path.name}"
        output_path = output_dir / output_name
        shutil.copy2(scan_record.path, output_path)

        items[identity.file_id] = {
            "file_id": identity.file_id,
            "source_root": source_key,
            "last_observed_path": str(scan_record.path),
            "last_observed_size_bytes": scan_record.size_bytes,
            "last_observed_mtime_ns": scan_record.mtime_ns,
            "partial_hash": identity.partial_hash,
            "fingerprint_version": identity.fingerprint_version,
            "media_kind": identity.media_kind.value,
            "copied_path": str(output_path),
            "copied_at": datetime.now(timezone.utc).isoformat(),
        }
        per_root_copied[source_key] += 1

    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest_path, manifest)

    results = [
        SampleResult(
            source_root=str(root),
            copied=per_root_copied[str(root)],
            skipped_existing=per_root_skipped[str(root)],
        )
        for root in source_roots
    ]
    print(json.dumps({"manifest_path": str(manifest_path), "results": [asdict(item) for item in results]}, indent=2))


if __name__ == "__main__":
    main()
