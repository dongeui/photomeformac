"""Video keyframe extraction service."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
from shutil import which

from app.core.contracts import DerivedAssetKind, DerivedAssetLocation
from app.services.processing.registry import build_derived_asset_location


@dataclass(frozen=True)
class VideoKeyframeConfig:
    derived_root: Path
    timestamps: tuple[float, ...] = (1.0,)
    version: str = "v1"


class VideoKeyframeService:
    def __init__(self, config: VideoKeyframeConfig) -> None:
        self._config = config

    def extract(self, source_path: Path, file_id: str) -> list[DerivedAssetLocation]:
        ffmpeg = which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required for keyframe extraction")

        locations: list[DerivedAssetLocation] = []
        for index, timestamp in enumerate(self._config.timestamps):
            location = build_derived_asset_location(
                self._config.derived_root,
                DerivedAssetKind.KEYFRAME,
                f"{file_id}-{index}",
                version=self._config.version,
            )
            location.absolute_path.parent.mkdir(parents=True, exist_ok=True)
            completed = None
            for candidate_timestamp in self._candidate_timestamps(timestamp):
                command = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    f"{candidate_timestamp:.3f}",
                    "-i",
                    str(source_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1024:1024:force_original_aspect_ratio=decrease",
                    str(location.absolute_path),
                ]
                completed = subprocess.run(command, capture_output=True, text=True, check=False)
                if completed.returncode == 0:
                    break
            if completed is None or completed.returncode != 0:
                raise RuntimeError((completed.stderr.strip() if completed is not None else "") or "ffmpeg keyframe extraction failed")
            locations.append(location)
        return locations

    def _candidate_timestamps(self, requested_timestamp: float) -> tuple[float, ...]:
        candidates: list[float] = [max(0.0, requested_timestamp)]
        if requested_timestamp > 0.0:
            candidates.append(0.0)
        deduped: list[float] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return tuple(deduped)
