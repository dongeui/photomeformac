"""DB-cached reverse geocoding wrapper."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.semantic import GeocodingCache
from app.services.geocoding import GeocodingProvider, GeocodingResult

logger = logging.getLogger(__name__)


class CachedGeocodingService:
    """Wraps any GeocodingProvider with a DB-backed cache keyed by coordinate precision.

    key = f"{lat:.{precision}f},{lon:.{precision}f}"  (default precision=3)
    """

    def __init__(
        self,
        session: Session,
        provider: GeocodingProvider,
        *,
        precision: int = 3,
    ) -> None:
        self._session = session
        self._provider = provider
        self._precision = precision

    def reverse(self, latitude: float, longitude: float) -> GeocodingResult | None:
        key = f"{latitude:.{self._precision}f},{longitude:.{self._precision}f}"

        cached = self._session.get(GeocodingCache, key)
        if cached is not None:
            return GeocodingResult(
                country=cached.country,
                region=cached.region,
                city=cached.city,
                place=cached.place,
                aliases=tuple(str(value) for value in (cached.aliases_json or []) if str(value).strip()),
                display_name=cached.display_name,
            )

        result = self._provider.reverse(latitude, longitude)
        if result is None:
            return None

        row = GeocodingCache(
            key=key,
            country=result.country,
            region=result.region,
            city=result.city,
            place=result.place,
            aliases_json=list(result.aliases),
            display_name=result.display_name,
        )
        self._session.merge(row)
        self._session.flush()
        return result
