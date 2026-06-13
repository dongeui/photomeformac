"""Moondream2 caption provider.

Install: pip install moondream
Model is downloaded automatically on first use (~1.8 GB for int8 variant).

Set TROVE_CAPTION_PROVIDER=moondream to activate.
Set TROVE_MOONDREAM_MODEL to override the model revision (default: 2025-01-09).
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.caption import CaptionProvider, CaptionResult
from app.services.image_decode import ensure_heif_support

logger = logging.getLogger(__name__)

_MOONDREAM_REVISION = os.environ.get("TROVE_MOONDREAM_MODEL", "2025-01-09")

# Guard against model loading more than once
_model_lock = threading.Lock()


@lru_cache(maxsize=1)
def _load_model() -> Any:
    """Load Moondream2 model (cached after first load)."""
    import moondream as md  # type: ignore[import]

    logger.info("Loading Moondream2 model (revision=%s)…", _MOONDREAM_REVISION)
    model = md.vl(model="moondream-2B-int8.mf.gz")
    logger.info("Moondream2 model loaded")
    return model


class Moondream2Provider:
    """VLM caption provider backed by Moondream2 (2B int8, local)."""

    provider_name = "moondream2"

    # Prompt templates
    _CAPTION_PROMPT = (
        "Describe this photo in one concise sentence. "
        "Focus on the main subject, setting, and any notable activity."
    )
    _OBJECTS_PROMPT = (
        "List the main objects and subjects visible in this photo as a "
        "comma-separated list. Be brief."
    )
    _ACTIVITY_PROMPT = (
        "What activity or event is happening in this photo? "
        "One short phrase, or 'none' if no clear activity."
    )
    _SETTING_PROMPT = (
        "Where was this photo taken? "
        "One short phrase: indoor/outdoor type, e.g. 'beach', 'cafe interior', 'city street'."
    )

    def caption(self, image_path: Path) -> CaptionResult | None:
        try:
            from PIL import Image  # noqa: F401 — ensure Pillow available
            import moondream as md  # type: ignore[import]
        except ImportError:
            logger.debug("moondream not installed; skipping caption")
            return None

        try:
            with _model_lock:
                model = _load_model()

            from PIL import Image

            ensure_heif_support()
            image = Image.open(image_path).convert("RGB")
            encoded = model.encode_image(image)

            short_caption = (model.query(encoded, self._CAPTION_PROMPT).get("answer") or "").strip()
            objects_raw = (model.query(encoded, self._OBJECTS_PROMPT).get("answer") or "").strip()
            activity_raw = (model.query(encoded, self._ACTIVITY_PROMPT).get("answer") or "").strip()
            setting_raw = (model.query(encoded, self._SETTING_PROMPT).get("answer") or "").strip()

            objects = [o.strip() for o in objects_raw.split(",") if o.strip() and o.strip().lower() != "none"]
            activities = [a.strip() for a in activity_raw.split(",") if a.strip() and a.strip().lower() != "none"]
            setting = setting_raw if setting_raw.lower() != "none" else ""

            return CaptionResult(
                short_caption=short_caption,
                objects=objects,
                activities=activities,
                setting=setting,
                provider=self.provider_name,
            )
        except Exception as exc:
            logger.warning("Moondream2 caption failed: %s", exc, extra={"path": str(image_path)})
            return None
