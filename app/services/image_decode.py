"""Image decoder registration helpers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_HEIF_REGISTERED = False


def ensure_heif_support() -> None:
    """Register HEIF/HEIC support when pillow-heif is installed.

    macOS native runs can fall back to `sips`, but Docker/Linux needs a Pillow
    decoder for HEIC originals before Phase 1 thumbnails or Phase 2 CLIP can
    read them.
    """
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _HEIF_REGISTERED = True
    except ImportError:
        logger.debug("pillow-heif not installed; HEIC decode fallback unavailable")
