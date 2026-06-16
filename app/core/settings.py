"""Application settings and runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from app.core.envcompat import env_value as _env_value
from app.core.envcompat import normalize_environment


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


def _env_optional_int(name: str) -> int | None:
    value = _env_value(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _cpu_count() -> int:
    return max(1, int(os.cpu_count() or 1))


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


def load_env_file() -> None:
    """`TROVE_ENV_FILE`(없으면 `.env`)의 KEY=VALUE를 os.environ에 채운다.

    대시보드에서 저장한 값(예: 성능 설정)이 앱/백엔드 재시작 후에도 살아남게
    한다. 이 파일은 쓰기만 되고 아무도 읽지 않아 재시작 시 설정이 초기화되던
    문제를 해결한다. 이미 프로세스 환경에 있는 키는 건드리지 않는다(setdefault).
    """
    configured = os.environ.get("TROVE_ENV_FILE", ".env")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        if not path.is_file():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _sep, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
    except OSError:
        return
    # 파일에서 들어온 레거시(PHOTOME_/PHOTOMINE_) 키도 TROVE_*로 승격.
    normalize_environment()


def asset_worker_cap() -> int:
    return max(1, min(16, _cpu_count()))


def torch_thread_cap() -> int:
    return max(1, _cpu_count())


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
    default_locale: str
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
    sync_scheduler_enabled: bool
    sync_scheduler_interval_seconds: int
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
    torch_threads: int | None
    semantic_maintenance_batch_size: int
    semantic_manual_batch_size: int
    lan_admin_token: str
    # opt-in 크래시 리포팅. 둘 다 채워졌을 때만 Sentry가 초기화된다(동의 + DSN).
    # mac 앱이 사용자 동의 시 TROVE_CRASH_REPORTING=1 + TROVE_SENTRY_DSN을 넘긴다.
    crash_reporting_enabled: bool
    sentry_dsn: str

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
        return Path(_env("TROVE_MODEL_ROOT", str(self.data_root / "models"))).expanduser().resolve()


def _env_float(name: str, default: float) -> float:
    value = _env_value(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a float") from exc


def load_settings() -> AppSettings:
    # 저장된 설정 파일을 먼저 환경에 반영해, 대시보드에서 바꾼 값이 재시작 후에도
    # 유지되게 한다.
    load_env_file()
    data_root = Path(_env("TROVE_DATA_ROOT", "./data")).expanduser().resolve()
    source_roots = _env_paths(
        "TROVE_SOURCE_ROOTS",
        default=_default_source_roots(),
    )
    source_root_host_raw = _env_value("TROVE_SOURCE_ROOT_HOST")
    source_root_mount_raw = _env_value("TROVE_SOURCE_ROOT_MOUNT")
    source_root_host = Path(source_root_host_raw).expanduser().resolve() if source_root_host_raw else None
    source_root_mount = Path(source_root_mount_raw).expanduser().resolve() if source_root_mount_raw else None
    derived_root = Path(_env("TROVE_DERIVED_ROOT", "./derived_root")).expanduser().resolve()
    database_path = Path(_env("TROVE_DATABASE_PATH", str(data_root / "photome.sqlite3"))).expanduser().resolve()
    database_url = _env("TROVE_DATABASE_URL", f"sqlite:///{database_path}")
    offline_mode = _env_bool("TROVE_OFFLINE_MODE", False)
    model_root = Path(_env("TROVE_MODEL_ROOT", str(data_root / "models"))).expanduser().resolve()
    geocoding_enabled = _env_bool("TROVE_GEOCODING_ENABLED", True)

    return AppSettings(
        app_name=_env("TROVE_APP_NAME", "trove"),
        app_version=_env("TROVE_APP_VERSION", "0.1.0"),
        # 기본 표시 언어. mac 앱 첫 실행 시 선택값을 TROVE_LOCALE로 넘긴다.
        # 비워두면(웹/도커 단독 실행 등) 웹이 쿠키 > Accept-Language > ko 순으로
        # 정한다. 명시 설정 시 그 값이 Accept-Language보다 우선한다.
        default_locale=_env("TROVE_LOCALE", ""),
        server_host=_env("TROVE_SERVER_HOST", "127.0.0.1"),
        server_port=_env_int("TROVE_SERVER_PORT", 8000),
        log_level=_env("TROVE_LOG_LEVEL", "INFO").upper(),
        reload=_env_bool("TROVE_RELOAD", False),
        offline_mode=offline_mode,
        data_root=data_root,
        source_roots=source_roots,
        source_root_host=source_root_host,
        source_root_mount=source_root_mount,
        derived_root=derived_root,
        geodata_root=Path(_env("TROVE_GEODATA_ROOT", str(model_root / "geodata"))).expanduser().resolve(),
        database_path=database_path,
        database_url=database_url,
        partial_hash_bytes=_env_int("TROVE_PARTIAL_HASH_BYTES", 1_048_576),
        thumbnail_size=_env_int("TROVE_THUMBNAIL_SIZE", 512),
        include_hidden_files=_env_bool("TROVE_INCLUDE_HIDDEN_FILES", False),
        stability_window_seconds=_env_int("TROVE_STABILITY_WINDOW_SECONDS", 300),
        # 통합 동기화(스캔+이미지 AI) 스케줄러. 옛 SEMANTIC_SCHEDULER_* 이름은
        # 레거시 별칭으로만 읽는다.
        sync_scheduler_enabled=_env_bool(
            "TROVE_SYNC_SCHEDULER_ENABLED",
            _env_bool("TROVE_SEMANTIC_SCHEDULER_ENABLED", True),
        ),
        sync_scheduler_interval_seconds=_env_int(
            "TROVE_SYNC_SCHEDULER_INTERVAL_SECONDS",
            _env_int("TROVE_SEMANTIC_SCHEDULER_INTERVAL_SECONDS", 600),
        ),
        semantic_ocr_enabled=_env_bool("TROVE_OCR_ENABLED", True),
        semantic_ocr_heuristic_enabled=_env_bool("TROVE_OCR_HEURISTIC_ENABLED", True),
        semantic_clip_enabled=_env_bool("TROVE_CLIP_ENABLED", False),
        semantic_clip_model_name=_env("TROVE_CLIP_MODEL_NAME", "ViT-B-32"),
        semantic_clip_pretrained=_env("TROVE_CLIP_PRETRAINED", "openai"),
        face_analysis_enabled=_env_bool("TROVE_FACE_ANALYSIS_ENABLED", True),
        face_detection_score_threshold=_env_float("TROVE_FACE_DETECTION_SCORE_THRESHOLD", 0.8),
        face_min_size=_env_int("TROVE_FACE_MIN_SIZE", 60),
        face_match_threshold=_env_float("TROVE_FACE_MATCH_THRESHOLD", 0.363),
        face_analysis_version=_env("TROVE_FACE_ANALYSIS_VERSION", "face-v2"),
        place_tag_precision=_env_int("TROVE_PLACE_TAG_PRECISION", 3),
        geocoding_enabled=geocoding_enabled,
        semantic_place_version=_env("TROVE_SEMANTIC_PLACE_VERSION", "place-v3"),
        semantic_person_version=_env("TROVE_SEMANTIC_PERSON_VERSION", "person-v1"),
        semantic_ocr_version=_env("TROVE_SEMANTIC_OCR_VERSION", "ocr-v1"),
        semantic_caption_version=_env("TROVE_SEMANTIC_CAPTION_VERSION", "caption-v1"),
        semantic_embedding_version=_env("TROVE_SEMANTIC_EMBEDDING_VERSION", "embedding-v1"),
        semantic_auto_tag_version=_env("TROVE_SEMANTIC_AUTO_TAG_VERSION", "auto-v2"),
        semantic_search_version=_env("TROVE_SEMANTIC_SEARCH_VERSION", "search-v4"),
        asset_processing_workers=_clamp(_env_int("TROVE_ASSET_PROCESSING_WORKERS", 1), 1, asset_worker_cap()),
        torch_threads=(
            _clamp(torch_threads, 1, torch_thread_cap())
            if (torch_threads := _env_optional_int("TROVE_TORCH_THREADS")) is not None
            else None
        ),
        semantic_maintenance_batch_size=_clamp(
            _env_int("TROVE_SEMANTIC_MAINTENANCE_BATCH_SIZE", 500),
            50,
            5000,
        ),
        semantic_manual_batch_size=_clamp(
            _env_int("TROVE_SEMANTIC_MANUAL_BATCH_SIZE", 1000),
            50,
            5000,
        ),
        lan_admin_token=_env("TROVE_LAN_ADMIN_TOKEN", ""),
        crash_reporting_enabled=_env_bool("TROVE_CRASH_REPORTING", False),
        sentry_dsn=_env("TROVE_SENTRY_DSN", ""),
    )
