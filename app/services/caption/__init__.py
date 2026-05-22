"""Caption provider interface for VLM-based image understanding."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CaptionResult:
    short_caption: str
    objects: list[str] = field(default_factory=list)
    activities: list[str] = field(default_factory=list)
    setting: str = ""
    provider: str = ""


class CaptionProvider(Protocol):
    """Interface for VLM caption providers.

    Implementations must be stateless across calls and safe for
    concurrent use from multiple threads.
    """

    @property
    def provider_name(self) -> str: ...

    def caption(self, image_path: Path) -> CaptionResult | None:
        """Return a caption for the image, or None on failure."""
        ...


__all__ = ["CaptionProvider", "CaptionResult"]
