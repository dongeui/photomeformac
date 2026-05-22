"""Application settings and runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _env_value(name: str) -> str | None:
    candidates = [name]
    if name.startswith("PHOTOMINE_"):
        candidates.insert(0, name.replace("PHOTOMINE_", "PHOTOME_", 1))
    for candidate in candidates:
        value = os.getenv(candidate)
        if value is not None and value != "":
            return value
    return None


def _env(name: str, default: str) -> str:
    value = _env_value(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    value = _env_value(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = _env_value(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_paths(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    value = _env_value(name)
    if value is None or value.strip() == "":
        return default

    paths = [Path(item.strip()).expanduser().resolve() for item in value.split(",") if item.strip()]
    if not paths:
        return default
    return tuple(paths)


def _default_source_roots() -> tuple[Path, ...]:
    return (Path("./photos").expanduser().resolve(),)


@dataclass(frozen=True)
class AppSettings:
    """Process-level settings for the FastAPI app."""

    app_name: str
    app_version: str
    server_host: str
    server_port: int
    log_level: str
    reload: bool
    offline_mode: bool
    data_root: Path
    source_roots: tuple[Path, ...]
    source_root_host: Path | None
    source_root_mount: Path | None
    derived_root: Path
    geodata_root: Path
    database_path: Path
    database_url: str
    partial_hash_bytes: int
    thumbnail_size: int
    include_hidden_files: bool
    stability_window_seconds: int
    scheduler_enabled: bool
    scheduler_poll_interval_seconds: int
    scheduler_daily_full_scan_hour: int
    scheduler_daily_full_scan_minute: int
    semantic_scheduler_enabled: bool
    semantic_scheduler_interval_seconds: int
    semantic_ocr_enabled: bool
    semantic_ocr_heuristic_enabled: bool
    semantic_clip_enabled: bool
    semantic_clip_model_name: str
    semantic_clip_pretrained: str
    face_analysis_enabled: bool
    face_detection_score_threshold: float
    face_min_size: int
    face_match_threshold: float
    face_analysis_version: str
    place_tag_precision: int
    geocoding_enabled: bool
    semantic_place_version: str
    semantic_person_version: str
    semantic_ocr_version: str
    semantic_caption_version: str
    semantic_embedding_version: str
    semantic_auto_tag_version: str
    semantic_search_version: str
    asset_processing_workers: int

    @property
    def thumbnail_root(self) -> Path:
        return self.derived_root / "thumb"

    @property
    def preview_root(self) -> Path:
        return self.derived_root / "preview"

    @property
    def keyframe_root(self) -> Path:
        return self.derived_root / "keyframe"

    @property
    def embeddings_root(self) -> Path:
        return self.derived_root / "embeddings"

    @property
    def model_root(self) -> Path:
        return Path(_env("PHOTOMINE_MODEL_ROOT", str(self.data_root / "models"))).expanduser().resolve()


def _env_float(name: str, default: float) -> float:
    value = _env_value(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a float") from exc


def load_settings() -> AppSettings:
    data_root = Path(_env("PHOTOMINE_DATA_ROOT", "./data")).expanduser().resolve()
    source_roots = _env_paths(
        "PHOTOMINE_SOURCE_ROOTS",
        default=_default_source_roots(),
    )
    source_root_host_raw = _env_value("PHOTOMINE_SOURCE_ROOT_HOST")
    source_root_mount_raw = _env_value("PHOTOMINE_SOURCE_ROOT_MOUNT")
    source_root_host = Path(source_root_host_raw).expanduser().resolve() if source_root_host_raw else None
    source_root_mount = Path(source_root_mount_raw).expanduser().resolve() if source_root_mount_raw else None
    derived_root = Path(_env("PHOTOMINE_DERIVED_ROOT", "./derived_root")).expanduser().resolve()
    database_path = Path(_env("PHOTOMINE_DATABASE_PATH", str(data_root / "photome.sqlite3"))).expanduser().resolve()
    database_url = _env("PHOTOMINE_DATABASE_URL", f"sqlite:///{database_path}")
    offline_mode = _env_bool("PHOTOMINE_OFFLINE_MODE", False)
    model_root = Path(_env("PHOTOMINE_MODEL_ROOT", str(data_root / "models"))).expanduser().resolve()
    geocoding_enabled = _env_bool("PHOTOMINE_GEOCODING_ENABLED", True)

    return AppSettings(
        app_name=_env("PHOTOMINE_APP_NAME", "photome"),
        app_version=_env("PHOTOMINE_APP_VERSION", "0.1.0"),
        server_host=_env("PHOTOMINE_SERVER_HOST", "127.0.0.1"),
        server_port=_env_int("PHOTOMINE_SERVER_PORT", 8000),
        log_level=_env("PHOTOMINE_LOG_LEVEL", "INFO").upper(),
        reload=_env_bool("PHOTOMINE_RELOAD", False),
        offline_mode=offline_mode,
        data_root=data_root,
        source_roots=source_roots,
        source_root_host=source_root_host,
        source_root_mount=source_root_mount,
        derived_root=derived_root,
        geodata_root=Path(_env("PHOTOMINE_GEODATA_ROOT", str(model_root / "geodata"))).expanduser().resolve(),
        database_path=database_path,
        database_url=database_url,
        partial_hash_bytes=_env_int("PHOTOMINE_PARTIAL_HASH_BYTES", 1_048_576),
        thumbnail_size=_env_int("PHOTOMINE_THUMBNAIL_SIZE", 512),
        include_hidden_files=_env_bool("PHOTOMINE_INCLUDE_HIDDEN_FILES", False),
        stability_window_seconds=_env_int("PHOTOMINE_STABILITY_WINDOW_SECONDS", 300),
        scheduler_enabled=_env_bool("PHOTOMINE_SCHEDULER_ENABLED", False),
        scheduler_poll_interval_seconds=_env_int("PHOTOMINE_SCHEDULER_POLL_INTERVAL_SECONDS", 900),
        scheduler_daily_full_scan_hour=_env_int("PHOTOMINE_SCHEDULER_DAILY_FULL_SCAN_HOUR", 3),
        scheduler_daily_full_scan_minute=_env_int("PHOTOMINE_SCHEDULER_DAILY_FULL_SCAN_MINUTE", 0),
        semantic_scheduler_enabled=_env_bool("PHOTOMINE_SEMANTIC_SCHEDULER_ENABLED", True),
        semantic_scheduler_interval_seconds=_env_int("PHOTOMINE_SEMANTIC_SCHEDULER_INTERVAL_SECONDS", 600),
        semantic_ocr_enabled=_env_bool("PHOTOMINE_OCR_ENABLED", True),
        semantic_ocr_heuristic_enabled=_env_bool("PHOTOMINE_OCR_HEURISTIC_ENABLED", True),
        semantic_clip_enabled=_env_bool("PHOTOMINE_CLIP_ENABLED", False),
        semantic_clip_model_name=_env("PHOTOMINE_CLIP_MODEL_NAME", "ViT-B-32"),
        semantic_clip_pretrained=_env("PHOTOMINE_CLIP_PRETRAINED", "openai"),
        face_analysis_enabled=_env_bool("PHOTOMINE_FACE_ANALYSIS_ENABLED", True),
        face_detection_score_threshold=_env_float("PHOTOMINE_FACE_DETECTION_SCORE_THRESHOLD", 0.8),
        face_min_size=_env_int("PHOTOMINE_FACE_MIN_SIZE", 60),
        face_match_threshold=_env_float("PHOTOMINE_FACE_MATCH_THRESHOLD", 0.363),
        face_analysis_version=_env("PHOTOMINE_FACE_ANALYSIS_VERSION", "face-v2"),
        place_tag_precision=_env_int("PHOTOMINE_PLACE_TAG_PRECISION", 3),
        geocoding_enabled=geocoding_enabled,
        semantic_place_version=_env("PHOTOMINE_SEMANTIC_PLACE_VERSION", "place-v3"),
        semantic_person_version=_env("PHOTOMINE_SEMANTIC_PERSON_VERSION", "person-v1"),
        semantic_ocr_version=_env("PHOTOMINE_SEMANTIC_OCR_VERSION", "ocr-v1"),
        semantic_caption_version=_env("PHOTOMINE_SEMANTIC_CAPTION_VERSION", "caption-v1"),
        semantic_embedding_version=_env("PHOTOMINE_SEMANTIC_EMBEDDING_VERSION", "embedding-v1"),
        semantic_auto_tag_version=_env("PHOTOMINE_SEMANTIC_AUTO_TAG_VERSION", "auto-v1"),
        semantic_search_version=_env("PHOTOMINE_SEMANTIC_SEARCH_VERSION", "search-v4"),
        asset_processing_workers=max(1, min(4, _env_int("PHOTOMINE_ASSET_PROCESSING_WORKERS", 1))),
    )
