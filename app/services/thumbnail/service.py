"""Thumbnail generation for image and video media."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
from shutil import which

from app.core.contracts import DerivedAssetKind, DerivedAssetLocation, MediaKind
from app.services.image_decode import ensure_heif_support
from app.services.processing.registry import build_derived_asset_location

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency fallback
    Image = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ThumbnailConfig:
    derived_root: Path
    size: int = 512
    version: str = "v1"


class ThumbnailService:
    def __init__(self, config: ThumbnailConfig) -> None:
        self._config = config

    def generate(self, source_path: Path, file_id: str, media_kind: MediaKind) -> DerivedAssetLocation:
        location = build_derived_asset_location(
            self._config.derived_root,
            DerivedAssetKind.THUMBNAIL,
            file_id,
            version=self._config.version,
        )
        location.absolute_path.parent.mkdir(parents=True, exist_ok=True)

        if media_kind == MediaKind.IMAGE:
            self._generate_image_thumbnail(source_path, location.absolute_path)
            return location

        if media_kind == MediaKind.VIDEO:
            self._generate_video_thumbnail(source_path, location.absolute_path)
            return location

        raise ValueError(f"Unsupported media kind for thumbnail generation: {media_kind}")

    def _generate_image_thumbnail(self, source_path: Path, output_path: Path) -> None:
        pillow_error: str | None = None
        if Image is not None:
            try:
                ensure_heif_support()
                with Image.open(source_path) as image:
                    image.thumbnail((self._config.size, self._config.size))
                    if image.mode not in {"RGB", "L"}:
                        converted = image.convert("RGB")
                    else:
                        converted = image
                    try:
                        converted.save(output_path, format="JPEG", quality=88, optimize=True)
                        return
                    finally:
                        if converted is not image:
                            converted.close()
            except Exception as exc:
                pillow_error = str(exc)
        else:
            pillow_error = "pillow is required for image thumbnail generation"

        sips = which("sips")
        if sips is not None:
            command = [
                sips,
                "-s",
                "format",
                "jpeg",
                "-Z",
                str(self._config.size),
                str(source_path),
                "--out",
                str(output_path),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode == 0:
                return
            sips_error = completed.stderr.strip() or completed.stdout.strip() or "sips thumbnail generation failed"
            if pillow_error:
                raise RuntimeError(f"{pillow_error}; {sips_error}")
            raise RuntimeError(sips_error)

        raise RuntimeError(pillow_error or "unable to generate image thumbnail")

    def _generate_video_thumbnail(self, source_path: Path, output_path: Path) -> None:
        ffmpeg = which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required for video thumbnail generation")

        command = [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-vf",
            f"thumbnail,scale={self._config.size}:{self._config.size}:force_original_aspect_ratio=decrease",
            "-frames:v",
            "1",
            str(output_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "ffmpeg thumbnail extraction failed")
