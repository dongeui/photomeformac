"""Core settings and shared runtime primitives."""

from app.core.contracts import (
    DerivedAssetKind,
    DerivedAssetLocation,
    FileIdentity,
    FileScanRecord,
    MediaKind,
    MediaMetadata,
    ProcessingJobKind,
    ProcessingJobState,
    media_kind_from_path,
    media_kind_from_suffix,
)
from app.core.logging import configure_logging
from app.core.settings import AppSettings, load_settings

__all__ = [
    "AppSettings",
    "DerivedAssetKind",
    "DerivedAssetLocation",
    "FileIdentity",
    "FileScanRecord",
    "MediaKind",
    "MediaMetadata",
    "ProcessingJobKind",
    "ProcessingJobState",
    "configure_logging",
    "load_settings",
    "media_kind_from_path",
    "media_kind_from_suffix",
]
