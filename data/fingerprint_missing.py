"""Direct-fingerprint missing files into media_files, bypassing scan discovery."""
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.processing.registry import MediaCatalog
from app.services.fingerprint.service import FingerprintService, FileScanRecord
from app.services.metadata.service import MetadataService
from app.core.contracts import MediaKind

DB = "/var/lib/trove/data/photome.sqlite3"
LIST_FILE = "/var/lib/trove/data/nas_image_list.txt"
SOURCE_ROOT = Path("/Volumes/homes/dejeong/Photos")
BATCH = 100

engine = create_engine(f"sqlite:///{DB}", connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)

fingerprint_svc = FingerprintService()
metadata_svc = MetadataService()

session = Session()
catalog = MediaCatalog(session)

# Load existing paths (NFC-normalized)
existing = set()
for row in session.execute(text("SELECT current_path FROM media_files")):
    existing.add(unicodedata.normalize("NFC", row[0]))
print(f"Existing in DB: {len(existing)}", flush=True)

# Load NAS list
with open(LIST_FILE) as f:
    nas_paths = [line.strip() for line in f if line.strip()]

missing = [p for p in nas_paths if unicodedata.normalize("NFC", p) not in existing]
print(f"Missing files: {len(missing)}", flush=True)

created = updated = failed = 0
now = datetime.utcnow()

for i, path_str in enumerate(missing, 1):
    path = Path(path_str)
    try:
        stat = path.stat()
        # Detect media kind
        ext = path.suffix.lower()
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"):
            kind = MediaKind.VIDEO
        else:
            kind = MediaKind.IMAGE

        scan_record = FileScanRecord(
            source_root=SOURCE_ROOT,
            path=path,
            relative_path=path.relative_to(SOURCE_ROOT),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            media_kind=kind,
        )

        identity = fingerprint_svc.fingerprint(scan_record)

        metadata_result = None
        try:
            metadata_result = metadata_svc.extract(scan_record)
        except Exception:
            pass

        change = catalog.upsert_media_file(
            scan_record,
            identity,
            metadata_result.metadata if metadata_result else None,
            now=now,
        )
        if change.action == "created":
            created += 1
        else:
            updated += 1

    except Exception as exc:
        failed += 1
        if failed <= 5:
            print(f"  FAIL {path_str[-50:]}: {exc}", flush=True)

    if i % BATCH == 0:
        session.commit()
        print(f"  {i}/{len(missing)} done: created={created} updated={updated} failed={failed}", flush=True)

session.commit()
session.close()
print(f"\nDONE: created={created} updated={updated} failed={failed}", flush=True)
