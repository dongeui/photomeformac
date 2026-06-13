"""Reverse geocoding service with DB-backed cache."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeocodingResult:
    country: str = ""
    region: str = ""
    city: str = ""
    place: str = ""
    aliases: tuple[str, ...] = ()
    display_name: str = ""

    def place_tags(self) -> list[str]:
        """Return non-empty location strings suitable for place tags."""
        seen: set[str] = set()
        result: list[str] = []
        for part in (self.place, self.city, self.region, self.country, *self.aliases):
            if part and part not in seen:
                seen.add(part)
                result.append(part)
        return result


class GeocodingProvider(Protocol):
    def reverse(self, latitude: float, longitude: float) -> GeocodingResult | None: ...


class NominatimProvider:
    """Free reverse geocoding via OpenStreetMap Nominatim public API.

    Rate-limited to 1 req/s per Nominatim usage policy.
    Set TROVE_NOMINATIM_URL to point at a self-hosted instance.
    """

    _MIN_INTERVAL = 1.1  # seconds between requests

    def __init__(self, user_agent: str = "trove/1.0") -> None:
        self._user_agent = user_agent
        self._last_request_time: float = 0.0

    def reverse(self, latitude: float, longitude: float) -> GeocodingResult | None:
        import urllib.request
        import urllib.parse
        import json
        import os

        base_url = os.environ.get("TROVE_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
        params = urllib.parse.urlencode({
            "lat": f"{latitude:.7f}",
            "lon": f"{longitude:.7f}",
            "format": "json",
            "zoom": 14,
            "addressdetails": 1,
        })
        url = f"{base_url}/reverse?{params}"

        # Rate limiting
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._MIN_INTERVAL:
            time.sleep(self._MIN_INTERVAL - elapsed)
        self._last_request_time = time.monotonic()

        try:
            req = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            logger.debug("Nominatim request failed: %s", exc)
            return None

        address = data.get("address", {})
        return GeocodingResult(
            country=address.get("country", ""),
            region=address.get("state", address.get("province", "")),
            city=address.get("city", address.get("town", address.get("village", ""))),
            place=address.get("suburb", address.get("neighbourhood", address.get("county", ""))),
            display_name=data.get("display_name", ""),
        )


__all__ = ["GeocodingProvider", "GeocodingResult", "NominatimProvider"]
