from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image


def test_sample_testbed_dedupes_by_file_id_not_path(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "testbed"
    manifest_path = tmp_path / "manifest.json"
    source_root.mkdir(parents=True, exist_ok=True)

    original = source_root / "sample.jpg"
    renamed = source_root / "renamed.jpg"
    create_image(original)

    run_sampler(source_root, dest_root, manifest_path)
    original.rename(renamed)
    run_sampler(source_root, dest_root, manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest["items"]

    assert len(items) == 1
    entry = next(iter(items.values()))
    assert entry["last_observed_path"] == str(renamed.resolve())
    assert len(list(dest_root.rglob("*.jpg"))) == 1


def run_sampler(source_root: Path, dest_root: Path, manifest_path: Path) -> None:
    completed = subprocess.run(
        [
            ".venv/bin/python",
            "scripts/sample_testbed.py",
            "--source-root",
            str(source_root),
            "--dest-root",
            str(dest_root),
            "--manifest-path",
            str(manifest_path),
            "--limit-per-root",
            "50",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)


def create_image(path: Path) -> None:
    image = Image.new("RGB", (32, 32), color=(100, 120, 140))
    image.save(path, format="JPEG")
