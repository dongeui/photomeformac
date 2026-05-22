"""Caption provider registry — select provider via PHOTOME_CAPTION_PROVIDER env var."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.services.caption import CaptionProvider, CaptionResult

logger = logging.getLogger(__name__)


def get_caption_provider() -> CaptionProvider | None:
    """Return the configured caption provider, or None if disabled.

    PHOTOME_CAPTION_PROVIDER:
      - 'moondream' (default when set): use Moondream2 local model
      - unset / 'none': caption generation disabled
    """
    if os.environ.get("PHOTOME_OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info("caption provider disabled in offline mode")
        return None

    provider_name = os.environ.get("PHOTOME_CAPTION_PROVIDER", "").strip().lower()
    if not provider_name or provider_name == "none":
        return None

    if provider_name == "moondream":
        from app.services.caption.moondream import Moondream2Provider
        return Moondream2Provider()

    logger.warning("Unknown caption provider '%s'; captioning disabled", provider_name)
    return None
