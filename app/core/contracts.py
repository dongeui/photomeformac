"""Shared contracts used across scanner, fingerprint, metadata, and processing layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import mimetypes
from pathlib import Path
from typing import Any


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    UNKNOWN = "unknown"


class DerivedAssetKind(str, Enum):
    THUMBNAIL = "thumb"
    PREVIEW = "preview"
    KEYFRAME = "keyframe"
    CLIP_EMBEDDING = "embedding_clip"


class ProcessingJobKind(str, Enum):
    SCAN = "scan"
    SEMANTIC_BACKFILL = "semantic_backfill"
    SEMANTIC_MAINTENANCE = "semantic_maintenance"
    THUMBNAIL = "thumbnail"
    KEYFRAME = "keyframe"
    PIPELINE = "pipeline"


class ProcessingJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


_IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".cr2",
    ".cr3",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

_VIDEO_SUFFIXES = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}


def media_kind_from_path(path: Path | str) -> MediaKind:
    suffix = Path(str(path)).suffix.casefold()
    if suffix in _IMAGE_SUFFIXES:
        return MediaKind.IMAGE
    if suffix in _VIDEO_SUFFIXES:
        return MediaKind.VIDEO

    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type is None:
        return MediaKind.UNKNOWN
    if mime_type.startswith("image/"):
        return MediaKind.IMAGE
    if mime_type.startswith("video/"):
        return MediaKind.VIDEO
    return MediaKind.UNKNOWN


def media_kind_from_suffix(suffix: str) -> MediaKind:
    normalized = suffix.lstrip(".")
    if not normalized:
        return MediaKind.UNKNOWN
    return media_kind_from_path(f"sample.{normalized}")


@dataclass(frozen=True)
class FileScanRecord:
    source_root: Path
    path: Path
    relative_path: Path
    size_bytes: int
    mtime_ns: int
    media_kind: MediaKind


@dataclass(frozen=True)
class FileIdentity:
    file_id: str
    size_bytes: int
    mtime_ns: int
    partial_hash: str
    fingerprint_version: str = "v1"
    media_kind: MediaKind = MediaKind.UNKNOWN


@dataclass(frozen=True)
class MediaMetadata:
    kind: MediaKind
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    codec_name: str | None = None
    mime_type: str | None = None
    captured_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaTagInput:
    tag_type: str
    tag_value: str


@dataclass(frozen=True)
class MediaFaceInput:
    bbox: dict[str, Any] = field(default_factory=dict)
    embedding_ref: str | None = None
    person_id: int | None = None
    person_display_name: str | None = None


@dataclass(frozen=True)
class DerivedAssetLocation:
    kind: DerivedAssetKind
    version: str
    relative_path: Path
    absolute_path: Path
