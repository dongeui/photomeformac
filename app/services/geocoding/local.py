"""Offline reverse geocoding from GeoNames and Natural Earth extracts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
from typing import Any, Iterable

from app.services.geocoding import GeocodingProvider, GeocodingResult

logger = logging.getLogger(__name__)

_MAX_CITY_DISTANCE_KM = 90.0
_EARTH_RADIUS_KM = 6371.0088


_COUNTRY_KO_ALIASES: dict[str, tuple[str, ...]] = {
    "KR": ("대한민국", "한국", "남한"),
    "JP": ("일본",),
    "CH": ("스위스",),
    "US": ("미국", "미합중국"),
    "CN": ("중국",),
    "TW": ("대만",),
    "HK": ("홍콩",),
    "SG": ("싱가포르",),
    "TH": ("태국",),
    "VN": ("베트남",),
    "PH": ("필리핀",),
    "ID": ("인도네시아",),
    "MY": ("말레이시아",),
    "FR": ("프랑스",),
    "IT": ("이탈리아",),
    "ES": ("스페인",),
    "DE": ("독일",),
    "GB": ("영국",),
    "CA": ("캐나다",),
    "AU": ("호주",),
    "NZ": ("뉴질랜드",),
}


@dataclass(frozen=True)
class _City:
    name: str
    ascii_name: str
    alternates: tuple[str, ...]
    latitude: float
    longitude: float
    country_code: str
    admin1_code: str


@dataclass(frozen=True)
class _Country:
    code: str
    name: str
    aliases: tuple[str, ...]
    geometry: dict[str, Any] | None = None
    bbox: tuple[float, float, float, float] | None = None


class LocalGazetteerProvider(GeocodingProvider):
    """Reverse geocode with local-only GeoNames city data and Natural Earth countries.

    Expected files under ``geodata_root``:
    - ``geonames/cities1000.txt`` or ``cities15000.txt``
    - ``geonames/admin1CodesASCII.txt`` optional
    - ``geonames/countryInfo.txt`` optional
    - ``naturalearth/countries.geojson`` optional
    """

    def __init__(self, geodata_root: Path | str) -> None:
        self._root = Path(geodata_root).expanduser().resolve()
        self._loaded = False
        self._cities_by_cell: dict[tuple[int, int], list[_City]] = {}
        self._admin1: dict[str, str] = {}
        self._countries: dict[str, _Country] = {}
        self._country_features: list[_Country] = []

    @property
    def ready(self) -> bool:
        self._ensure_loaded()
        return bool(self._cities_by_cell or self._country_features or self._countries)

    def reverse(self, latitude: float, longitude: float) -> GeocodingResult | None:
        self._ensure_loaded()
        if not self.ready:
            return None

        country = self._country_for_point(latitude, longitude)
        city, distance_km = self._nearest_city(latitude, longitude)
        if city is not None and distance_km > _MAX_CITY_DISTANCE_KM:
            city = None

        country_code = city.country_code if city is not None else (country.code if country else "")
        country_info = self._countries.get(country_code) or country
        region = self._admin1.get(f"{country_code}.{city.admin1_code}", "") if city is not None else ""

        aliases: list[str] = []
        if country_info is not None:
            aliases.extend(country_info.aliases)
        if city is not None:
            aliases.extend(_usable_names([city.name, city.ascii_name, *city.alternates], limit=8))

        country_name = country_info.name if country_info is not None else ""
        city_name = city.name if city is not None else ""
        display_parts = [part for part in (city_name, region, country_name) if part]
        if not display_parts and country_info is None:
            return None
        return GeocodingResult(
            country=country_name,
            region=region,
            city=city_name,
            aliases=tuple(_dedupe(aliases)),
            display_name=", ".join(display_parts),
        )

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._load_countries()
        self._load_admin1()
        self._load_cities()

    def _load_cities(self) -> None:
        candidates = [
            self._root / "geonames" / "cities1000.txt",
            self._root / "geonames" / "cities15000.txt",
        ]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            logger.info("local GeoNames city file not found", extra={"geodata_root": str(self._root)})
            return

        loaded = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 11:
                    continue
                try:
                    lat = float(parts[4])
                    lon = float(parts[5])
                except ValueError:
                    continue
                city = _City(
                    name=parts[1],
                    ascii_name=parts[2],
                    alternates=tuple(_parse_alternates(parts[3])),
                    latitude=lat,
                    longitude=lon,
                    country_code=parts[8],
                    admin1_code=parts[10],
                )
                self._cities_by_cell.setdefault(_cell(lat, lon), []).append(city)
                loaded += 1
        logger.info("local GeoNames cities loaded", extra={"path": str(path), "count": loaded})

    def _load_admin1(self) -> None:
        path = self._root / "geonames" / "admin1CodesASCII.txt"
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3:
                    self._admin1[parts[0]] = parts[1] or parts[2]

    def _load_countries(self) -> None:
        country_info_path = self._root / "geonames" / "countryInfo.txt"
        if country_info_path.is_file():
            with country_info_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip() or line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 5:
                        continue
                    code = parts[0]
                    name = parts[4]
                    aliases = _COUNTRY_KO_ALIASES.get(code, ())
                    self._countries[code] = _Country(code=code, name=name, aliases=tuple(_dedupe([name, *aliases])))

        geojson_path = self._root / "naturalearth" / "countries.geojson"
        if not geojson_path.is_file():
            return
        try:
            payload = json.loads(geojson_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Natural Earth GeoJSON load failed", extra={"path": str(geojson_path), "error": str(exc)})
            return
        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            geometry = feature.get("geometry")
            if not geometry:
                continue
            code = str(props.get("ISO_A2") or props.get("iso_a2") or props.get("ADM0_A3") or "")
            if len(code) != 2:
                code = str(props.get("WB_A2") or props.get("SU_A2") or "")
            if len(code) != 2:
                continue
            names = _usable_names(
                [
                    props.get("NAME"),
                    props.get("NAME_LONG"),
                    props.get("ADMIN"),
                    props.get("NAME_EN"),
                    props.get("NAME_KO"),
                    *_COUNTRY_KO_ALIASES.get(code, ()),
                ],
                limit=8,
            )
            country = _Country(
                code=code,
                name=names[0] if names else code,
                aliases=tuple(_dedupe(names)),
                geometry=geometry,
                bbox=_geometry_bbox(geometry),
            )
            self._country_features.append(country)
            self._countries.setdefault(code, country)

    def _nearest_city(self, latitude: float, longitude: float) -> tuple[_City | None, float]:
        base_lat, base_lon = _cell(latitude, longitude)
        best: _City | None = None
        best_distance = float("inf")
        for radius in range(0, 3):
            for dlat in range(-radius, radius + 1):
                for dlon in range(-radius, radius + 1):
                    if radius and abs(dlat) != radius and abs(dlon) != radius:
                        continue
                    for city in self._cities_by_cell.get((base_lat + dlat, base_lon + dlon), []):
                        distance = _haversine(latitude, longitude, city.latitude, city.longitude)
                        if distance < best_distance:
                            best = city
                            best_distance = distance
            if radius >= 1 and best is not None and best_distance <= 35:
                break
        return best, best_distance

    def _country_for_point(self, latitude: float, longitude: float) -> _Country | None:
        for country in self._country_features:
            if country.bbox is None or country.geometry is None:
                continue
            min_lon, min_lat, max_lon, max_lat = country.bbox
            if not (min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat):
                continue
            if _geometry_contains(country.geometry, longitude, latitude):
                return country
        return None


class ChainedGeocodingProvider(GeocodingProvider):
    """Try local providers first, then optional online providers."""

    def __init__(self, providers: Iterable[GeocodingProvider]) -> None:
        self._providers = tuple(providers)

    def reverse(self, latitude: float, longitude: float) -> GeocodingResult | None:
        for provider in self._providers:
            result = provider.reverse(latitude, longitude)
            if result is not None and result.place_tags():
                return result
        return None


def _cell(latitude: float, longitude: float) -> tuple[int, int]:
    return math.floor(latitude), math.floor(longitude)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_alternates(raw: str) -> list[str]:
    if not raw:
        return []
    return _usable_names(raw.split(","), limit=24)


def _usable_names(values: Iterable[Any], *, limit: int) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or len(text) > 80:
            continue
        if any(char.isdigit() for char in text) and not any("\uac00" <= char <= "\ud7a3" for char in text):
            continue
        result.append(text)
        if len(result) >= limit:
            break
    return _dedupe(result)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = value.strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _geometry_bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float] | None:
    coords = list(_iter_points(geometry.get("coordinates"), geometry.get("type")))
    if not coords:
        return None
    lons = [lon for lon, _ in coords]
    lats = [lat for _, lat in coords]
    return min(lons), min(lats), max(lons), max(lats)


def _iter_points(coords: Any, geometry_type: str | None):
    if geometry_type == "Polygon":
        for ring in coords or []:
            for point in ring:
                if len(point) >= 2:
                    yield float(point[0]), float(point[1])
    elif geometry_type == "MultiPolygon":
        for polygon in coords or []:
            for ring in polygon:
                for point in ring:
                    if len(point) >= 2:
                        yield float(point[0]), float(point[1])


def _geometry_contains(geometry: dict[str, Any], lon: float, lat: float) -> bool:
    geometry_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return _polygon_contains(coords, lon, lat)
    if geometry_type == "MultiPolygon":
        return any(_polygon_contains(polygon, lon, lat) for polygon in coords or [])
    return False


def _polygon_contains(polygon: Any, lon: float, lat: float) -> bool:
    rings = polygon or []
    if not rings or not _ring_contains(rings[0], lon, lat):
        return False
    return not any(_ring_contains(hole, lon, lat) for hole in rings[1:])


def _ring_contains(ring: Any, lon: float, lat: float) -> bool:
    inside = False
    points = [(float(point[0]), float(point[1])) for point in ring if len(point) >= 2]
    if len(points) < 3:
        return False
    j = len(points) - 1
    for i, (xi, yi) in enumerate(points):
        xj, yj = points[j]
        intersects = (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        if intersects:
            inside = not inside
        j = i
    return inside
