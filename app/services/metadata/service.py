"""Metadata extraction interfaces and default implementation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import json
import mimetypes
from numbers import Number
from pathlib import Path
from shutil import which
import subprocess
from typing import Any, Mapping, Sequence

from app.core.contracts import FileScanRecord, MediaKind, MediaMetadata
from app.services.image_decode import ensure_heif_support

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency fallback
    Image = None  # type: ignore[assignment]


GPS_INFO_TAG = 34853
GPS_LATITUDE_REF_TAG = 1
GPS_LATITUDE_TAG = 2
GPS_LONGITUDE_REF_TAG = 3
GPS_LONGITUDE_TAG = 4
GPS_ALTITUDE_REF_TAG = 5
GPS_ALTITUDE_TAG = 6
GPS_TIMESTAMP_TAG = 7
GPS_MAP_DATUM_TAG = 18
GPS_DATESTAMP_TAG = 29


@dataclass(frozen=True)
class MetadataResult:
    metadata: MediaMetadata
    warnings: tuple[str, ...] = ()


class MetadataService:
    def extract(self, scan_record: FileScanRecord) -> MetadataResult:
        mime_type, _ = mimetypes.guess_type(str(scan_record.path))
        if scan_record.media_kind == MediaKind.IMAGE:
            return self._extract_image(scan_record.path, mime_type)
        if scan_record.media_kind == MediaKind.VIDEO:
            return self._extract_video(scan_record.path, mime_type)

        image_result = self._extract_image(scan_record.path, mime_type)
        if image_result.metadata.width is not None or image_result.metadata.height is not None:
            return image_result

        video_result = self._extract_video(scan_record.path, mime_type)
        if video_result.metadata.width is not None or video_result.metadata.height is not None:
            return video_result

        return MetadataResult(
            metadata=MediaMetadata(kind=MediaKind.UNKNOWN, mime_type=mime_type),
            warnings=image_result.warnings + video_result.warnings,
        )

    def _extract_image(self, path: Path, mime_type: str | None) -> MetadataResult:
        warnings: list[str] = []
        if Image is None:
            warnings.append("pillow not available")
        else:
            extra: dict[str, Any] = {}
            raw_exif_payload: dict[str, Any] | None = None
            try:
                ensure_heif_support()
                with Image.open(path) as image:
                    try:
                        exif = image.getexif()
                        if exif:
                            raw_exif_payload = {str(key): exif.get(key) for key in exif.keys()}
                            extra["exif"] = _json_safe(raw_exif_payload)
                            gps_payload = _extract_exif_gps(exif)
                            if gps_payload is not None:
                                extra["gps"] = gps_payload
                    except Exception as exc:  # pragma: no cover - library-specific corruption path
                        extra["exif_warning"] = str(exc)
                    extra["format"] = _json_safe(image.format)
                    extra["mode"] = _json_safe(image.mode)
                    return MetadataResult(
                        metadata=MediaMetadata(
                            kind=MediaKind.IMAGE,
                            width=image.width,
                            height=image.height,
                            mime_type=mime_type,
                            captured_at=_extract_image_datetime(raw_exif_payload or extra.get("exif")),
                            extra=_json_safe(extra),
                        )
                    )
            except Exception as exc:
                warnings.append(str(exc))

        sips_result = self._extract_image_with_sips(path, mime_type)
        if sips_result is not None:
            if sips_result.metadata.width is not None or sips_result.metadata.height is not None:
                return sips_result
            return MetadataResult(
                metadata=sips_result.metadata,
                warnings=tuple(warnings + list(sips_result.warnings)),
            )

        return MetadataResult(
            metadata=MediaMetadata(kind=MediaKind.IMAGE, mime_type=mime_type),
            warnings=tuple(warnings),
        )

    def _extract_video(self, path: Path, mime_type: str | None) -> MetadataResult:
        ffprobe = self._find_tool("ffprobe")
        if ffprobe is None:
            return MetadataResult(
                metadata=MediaMetadata(kind=MediaKind.VIDEO, mime_type=mime_type),
                warnings=("ffprobe not available",),
            )

        command = [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return MetadataResult(
                metadata=MediaMetadata(kind=MediaKind.VIDEO, mime_type=mime_type),
                warnings=(completed.stderr.strip() or "ffprobe failed",),
            )

        try:
            payload = json.loads(completed.stdout)
        except Exception as exc:
            return MetadataResult(
                metadata=MediaMetadata(kind=MediaKind.VIDEO, mime_type=mime_type),
                warnings=(f"invalid ffprobe output: {exc}",),
            )
        streams = payload.get("streams", [])
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        format_info = payload.get("format", {})
        metadata = MediaMetadata(
            kind=MediaKind.VIDEO,
            width=video_stream.get("width"),
            height=video_stream.get("height"),
            duration_seconds=_parse_float(format_info.get("duration")),
            codec_name=video_stream.get("codec_name"),
            mime_type=mime_type,
            captured_at=_parse_video_datetime(payload),
            extra=_json_safe({
                "format_name": format_info.get("format_name"),
                "bit_rate": format_info.get("bit_rate"),
            }),
        )
        return MetadataResult(metadata=metadata)

    def _find_tool(self, name: str) -> str | None:
        return which(name)

    def _extract_image_with_sips(self, path: Path, mime_type: str | None) -> MetadataResult | None:
        sips = self._find_tool("sips")
        if sips is None:
            return None

        command = [sips, "-g", "pixelWidth", "-g", "pixelHeight", str(path)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            warning = completed.stderr.strip() or completed.stdout.strip() or "sips image probe failed"
            return MetadataResult(
                metadata=MediaMetadata(kind=MediaKind.IMAGE, mime_type=mime_type),
                warnings=(warning,),
            )

        width, height = _parse_sips_dimensions(completed.stdout)
        if width is None and height is None:
            return MetadataResult(
                metadata=MediaMetadata(kind=MediaKind.IMAGE, mime_type=mime_type),
                warnings=("sips did not report image dimensions",),
            )

        return MetadataResult(
            metadata=MediaMetadata(
                kind=MediaKind.IMAGE,
                width=width,
                height=height,
                mime_type=mime_type,
                extra={"probe": "sips"},
            )
        )


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping_get(payload: Mapping[Any, Any], *keys: Any) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
        string_key = str(key)
        if string_key in payload:
            return payload[string_key]
    return None


def _parse_sips_dimensions(stdout: str) -> tuple[int | None, int | None]:
    width = height = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, raw_value = [part.strip() for part in stripped.split(":", 1)]
        if key == "pixelWidth":
            width = _parse_int(raw_value)
        elif key == "pixelHeight":
            height = _parse_int(raw_value)
    return width, height


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _extract_image_datetime(exif_payload: Any) -> datetime | None:
    if not isinstance(exif_payload, Mapping):
        return None
    for key in (36867, 36868, 306):
        value = _mapping_get(exif_payload, key)
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _extract_exif_gps(exif_payload: Any) -> dict[str, Any] | None:
    gps_payload = _load_gps_payload(exif_payload)
    if gps_payload is None:
        return None

    latitude = _parse_gps_coordinate(
        _mapping_get(gps_payload, GPS_LATITUDE_TAG),
        _mapping_get(gps_payload, GPS_LATITUDE_REF_TAG),
    )
    longitude = _parse_gps_coordinate(
        _mapping_get(gps_payload, GPS_LONGITUDE_TAG),
        _mapping_get(gps_payload, GPS_LONGITUDE_REF_TAG),
    )
    if latitude is None or longitude is None:
        return None

    normalized: dict[str, Any] = {
        "source": "exif",
        "latitude": latitude,
        "longitude": longitude,
    }
    altitude = _parse_gps_altitude(
        _mapping_get(gps_payload, GPS_ALTITUDE_TAG),
        _mapping_get(gps_payload, GPS_ALTITUDE_REF_TAG),
    )
    if altitude is not None:
        normalized["altitude_m"] = altitude
    timestamp = _parse_gps_timestamp(gps_payload)
    if timestamp is not None:
        normalized["timestamp_utc"] = timestamp
    map_datum = _parse_text(_mapping_get(gps_payload, GPS_MAP_DATUM_TAG))
    if map_datum is not None:
        normalized["map_datum"] = map_datum
    return normalized


def _load_gps_payload(exif_payload: Any) -> Mapping[Any, Any] | None:
    get_ifd = getattr(exif_payload, "get_ifd", None)
    if callable(get_ifd):
        try:
            gps_payload = get_ifd(GPS_INFO_TAG)
        except Exception:  # pragma: no cover - pillow implementation detail
            gps_payload = None
        if isinstance(gps_payload, Mapping):
            return gps_payload
    if isinstance(exif_payload, Mapping):
        gps_payload = _mapping_get(exif_payload, GPS_INFO_TAG)
        if isinstance(gps_payload, Mapping):
            return gps_payload
    return None


def _parse_gps_coordinate(value: Any, ref: Any) -> float | None:
    coordinate = None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and len(value) >= 3:
        degrees = _parse_float(value[0])
        minutes = _parse_float(value[1])
        seconds = _parse_float(value[2])
        if degrees is None or minutes is None or seconds is None:
            return None
        coordinate = abs(degrees) + (minutes / 60.0) + (seconds / 3600.0)
    else:
        coordinate = _parse_float(value)
        if coordinate is None:
            return None
        coordinate = abs(coordinate)

    ref_text = (_parse_text(ref) or "").upper()
    if ref_text in {"S", "W"}:
        coordinate *= -1
    return round(coordinate, 7)


def _parse_gps_altitude(value: Any, ref: Any) -> float | None:
    altitude = _parse_float(value)
    if altitude is None:
        return None
    if _parse_int(ref) == 1:
        altitude *= -1
    return round(altitude, 2)


def _parse_gps_timestamp(gps_payload: Mapping[Any, Any]) -> str | None:
    datestamp = _parse_text(_mapping_get(gps_payload, GPS_DATESTAMP_TAG))
    timestamp_value = _mapping_get(gps_payload, GPS_TIMESTAMP_TAG)
    if datestamp is None or timestamp_value is None:
        return None

    try:
        gps_date = datetime.strptime(datestamp, "%Y:%m:%d").date()
    except ValueError:
        return None

    if not isinstance(timestamp_value, Sequence) or isinstance(timestamp_value, (str, bytes, bytearray)) or len(timestamp_value) < 3:
        return None
    hours = _parse_float(timestamp_value[0])
    minutes = _parse_float(timestamp_value[1])
    seconds = _parse_float(timestamp_value[2])
    if hours is None or minutes is None or seconds is None:
        return None

    timestamp = datetime(
        gps_date.year,
        gps_date.month,
        gps_date.day,
        tzinfo=timezone.utc,
    ) + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return timestamp.isoformat()


def _parse_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_video_datetime(payload: Mapping[str, Any]) -> datetime | None:
    streams = payload.get("streams", [])
    for stream in streams:
        if not isinstance(stream, Mapping):
            continue
        parsed = _parse_datetime_from_mapping(stream.get("tags"))
        if parsed is not None:
            return parsed
    format_info = payload.get("format")
    if isinstance(format_info, Mapping):
        return _parse_datetime_from_mapping(format_info.get("tags"))
    return None


def _parse_datetime_from_mapping(payload: Any) -> datetime | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("creation_time", "Creation Time", "com.apple.quicktime.creationdate"):
        parsed = _parse_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None

    candidates = (text, text.replace("Z", "+00:00"), text.replace(":", "-", 2))
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue

    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        items = [(str(key), _json_safe(item)) for key, item in value.items()]
        return {key: item for key, item in sorted(items, key=lambda pair: pair[0])}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Sequence):
        return [_json_safe(item) for item in value]
    if isinstance(value, Number):
        try:
            integer_value = int(value)
            if integer_value == value:
                return integer_value
            return float(value)
        except Exception:
            return str(value)
    for caster in (int, float):
        try:
            return caster(value)
        except Exception:
            continue
    return str(value)
