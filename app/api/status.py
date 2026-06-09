"""Runtime status endpoint and server-rendered dashboard."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from html import escape
import json
import os
from pathlib import Path
import re as _re

_INTERNAL_PERSON_ID_RE = _re.compile(r"^person-\d+$", _re.IGNORECASE)
import shlex
from shutil import which
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.ai_pack import get_ai_pack_state
from app.api.deps import require_state
from app.api.performance_settings import resource_settings_snapshot
from app.api.serializers import serialize_scheduler_snapshot
from app.core.settings import AppSettings
from app.services.analysis.opencv_zoo import SFACE_MODEL, YU_NET_MODEL, _is_valid_model_file
from app.services.embedding import clip as clip_embedding
from app.services.processing.registry import MediaCatalog
from app.models.asset import DerivedAsset
from app.models.face import Face
from app.models.job import ProcessingJob
from app.models.media import MediaFile
from app.models.person import Person
from app.models.semantic import MediaAutoTagState, MediaEmbedding, SearchDocument
from app.models.tag import Tag
from sqlalchemy import func, or_, select


router = APIRouter(tags=["status"])
_SECURITY_CACHE_TTL = timedelta(seconds=30)
_SECURITY_CACHE: tuple[tuple[Any, ...], datetime, dict[str, Any]] | None = None


@router.get("/source-roots/browse")
async def browse_source_roots(request: Request, path: Optional[str] = None) -> dict[str, Any]:
    """Small local folder browser for choosing Phase 1 source roots.

    Browser JavaScript cannot read absolute Finder paths reliably. This endpoint
    lists directories from the server process view instead, which works for both
    native macOS runtime and Docker-mounted paths.
    """
    settings: AppSettings = require_state(request, "settings")
    database = require_state(request, "database")
    if path:
        target = Path(path).expanduser()
    else:
        return {
            "path": None,
            "parent": None,
            "entries": _source_root_shortcuts(settings, database),
            "note": _source_browser_note(),
        }

    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve folder: {target}") from exc
    if not _is_allowed_source_browser_path(settings, database, resolved):
        raise HTTPException(status_code=403, detail="Folder is outside configured source browsing roots")
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder does not exist: {resolved}")

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(
            (child for child in resolved.iterdir() if child.is_dir() and _show_folder(child)),
            key=lambda child: child.name.casefold(),
        )
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"Cannot read folder: {resolved}") from exc

    for child in children[:250]:
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "kind": "directory",
                "selectable": True,
            }
        )
    return {
        "path": str(resolved),
        "parent": str(resolved.parent) if resolved.parent != resolved else None,
        "entries": entries,
        "truncated": len(children) > len(entries),
        "note": _source_browser_note(),
    }


def _show_folder(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return False
    return name not in {"__pycache__", "node_modules", ".git"}


def _source_browser_note() -> str:
    if _is_docker_runtime():
        return "Docker can browse Finder-style paths under /Volumes and /Users, so NAS mounts, external drives, USB storage, and local folders can all be selected."
    return "Native/local runtime can browse folders visible to this Mac user."


def _display_source_root_path(settings: AppSettings, value: str) -> str:
    path = Path(value)
    host_root = settings.source_root_host
    mount_root = settings.source_root_mount
    if host_root is None or mount_root is None:
        return value
    try:
        relative = path.relative_to(mount_root)
    except ValueError:
        return value
    return str((host_root / relative).expanduser())


def _display_source_roots(settings: AppSettings, values: list[str]) -> list[str]:
    return [_display_source_root_path(settings, value) for value in values]


def _path_exists(path: Path) -> bool:
    """Return whether a path exists without letting flaky mounts break dashboard rendering."""
    try:
        return path.exists()
    except OSError:
        return False


def _preferred_input_source_roots(
    settings: AppSettings,
    configured: list[str],
    known: list[str],
) -> list[str]:
    display_configured = _display_source_roots(settings, configured)
    host_like_known = [path for path in known if path.startswith("/Volumes/") or path.startswith("/Users/")]
    if host_like_known:
        if _is_docker_runtime():
            # Docker can't reach NAS/host paths — only show them if they actually exist inside the container.
            accessible = [p for p in host_like_known if _path_exists(Path(p))]
            if accessible:
                return accessible
            # Fall through: show the configured Docker mount paths instead.
        else:
            return host_like_known
    return display_configured


def _source_root_shortcuts(settings: AppSettings, database: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in _source_browser_roots(settings, database):
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(key)
        entries.append(
            {
                "name": _source_shortcut_name(resolved),
                "path": key,
                "kind": "shortcut",
                "selectable": True,
            }
        )
    return entries


def _source_browser_roots(settings: AppSettings, database: Any) -> list[Path]:
    home = Path.home()
    candidates: list[Path] = [
        home / "Pictures",
        home / "Photos",
        home / "Desktop",
        Path("/Volumes"),
        Path("/photos"),
    ]
    candidates.extend(settings.source_roots)
    with database.session_factory() as session:
        known = session.execute(select(MediaFile.source_root).distinct()).scalars().all()
    candidates.extend(Path(path) for path in known if path)
    return candidates


def _is_allowed_source_browser_path(settings: AppSettings, database: Any, path: Path) -> bool:
    for root in _source_browser_roots(settings, database):
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            continue
        try:
            path.relative_to(resolved_root)
            return True
        except ValueError:
            continue
    return False


def _source_shortcut_name(path: Path) -> str:
    if str(path) == "/Volumes":
        return "Finder 저장장치 · NAS/외장하드/USB"
    if str(path) == "/Users":
        return "Mac 사용자 폴더 · Desktop/Pictures/Photos"
    if str(path) == "/photos":
        return "Docker 내부 사진 폴더 · /photos"
    return path.name or str(path)


def _is_docker_runtime() -> bool:
    return os.path.exists("/.dockerenv")


def _deployment_label() -> str:
    return "Docker container" if _is_docker_runtime() else "Native local process"


def _source_root_guidance(settings: AppSettings) -> str:
    if _is_docker_runtime():
        if settings.source_root_host is not None and settings.source_root_mount is not None:
            return (
                "도커 실행이어도 파인더 경로를 그대로 입력하면 됩니다. NAS, 외장하드, USB, 로컬 폴더를 모두 받을 수 있고, "
                "Photome가 필요하면 해당 경로를 컨테이너 마운트 경로로 자동 변환합니다."
            )
        return (
            "도커 실행이어도 파인더 경로를 그대로 입력하면 됩니다. NAS, 외장하드, USB, 로컬 폴더는 /Volumes 와 /Users 마운트로 바로 읽고, "
            "별도 source mount는 host 원본 경로와 컨테이너 마운트 경로 설정으로 자동 변환할 수 있습니다."
        )
    return (
        "맥 로컬 실행에서는 파인더에서 보이는 일반 경로를 사용할 수 있습니다. NAS 공유는 보통 /Volumes 아래에 표시됩니다."
    )


def _schedule_label(hours: int | None) -> str:
    return "꺼짐" if hours is None else f"{hours}시간"


def _phase_state_label(value: str) -> str:
    return {
        "RUNNING": "실행 중",
        "WAITING": "대기",
        "IDLE": "대기 중",
    }.get(value, value)


def _dashboard_job_progress(job: dict[str, Any] | None) -> str:
    if not job:
        return "대기 중"
    result = job.get("result") or {}
    progress = result.get("progress") or {}
    kind = str(job.get("job_kind") or "")
    status_name = str(job.get("status") or "unknown")

    if kind == "scan":
        scan = progress.get("scan") or {}
        if scan.get("total") is not None:
            return (
                f"스캔 중 · {scan.get('current', 0)} / {scan.get('total')} · "
                f"발견 {progress.get('files_found', scan.get('total'))} · 실패 {scan.get('failed', 0)}"
            )
        processed = progress.get("processed") or {}
        if processed.get("total") is not None:
            return (
                f"처리 중 · {processed.get('current', 0)} / {processed.get('total')} · "
                f"완료 {processed.get('succeeded', 0)} · 실패 {processed.get('failed', 0)}"
            )
        summary = progress.get("summary") or {}
        if summary.get("scanned") is not None:
            return f"스캔 중 · 발견 {summary.get('scanned')} · 실패 {summary.get('failed', 0)}"
        return f"처리 중 · {progress.get('stage') or progress.get('message') or '작업 중'}"

    chunk = progress.get("chunk")
    pending = progress.get("pending")
    current = progress.get("current")
    total_done = progress.get("total_succeeded", progress.get("succeeded", 0))
    total_failed = progress.get("total_failed", progress.get("failed", 0))
    total_embeddings = progress.get("total_embeddings_created", progress.get("embeddings_created", 0))
    total_tags = progress.get("total_auto_tag_values", progress.get("auto_tag_values", 0))
    parts = ["검색 분석 중"]
    if chunk is not None:
        parts.append(f"묶음 {chunk}")
    if pending is not None or current is not None:
        parts.append(f"{current or 0} / {pending or current or 0}")
    parts.append(f"완료 {total_done}")
    parts.append(f"실패 {total_failed}")
    parts.append(f"AI +{total_embeddings}")
    parts.append(f"태그 +{total_tags}")
    return " · ".join(parts)


def _security_snapshot(settings: AppSettings) -> dict[str, Any]:
    global _SECURITY_CACHE
    model_root = settings.model_root
    cache_key = (
        settings.offline_mode,
        settings.semantic_clip_enabled,
        settings.semantic_clip_model_name,
        settings.semantic_clip_pretrained,
        str(model_root),
        str(settings.geodata_root),
    )
    now = datetime.utcnow()
    if _SECURITY_CACHE is not None:
        cached_key, cached_at, cached_payload = _SECURITY_CACHE
        if cached_key == cache_key and now - cached_at < _SECURITY_CACHE_TTL:
            return deepcopy(cached_payload)

    detector_path = model_root / YU_NET_MODEL.relative_path
    recognizer_path = model_root / SFACE_MODEL.relative_path
    face_models_ready = _is_valid_model_file(detector_path) and _is_valid_model_file(recognizer_path)
    clip_status = clip_embedding.status()
    clip_dependencies = clip_status.get("dependencies") or {}
    clip_dependency_ready = all(
        clip_dependencies.get(name) == "installed"
        for name in ("open_clip_torch", "torch", "torchvision")
    )
    if not settings.semantic_clip_enabled:
        clip_state = "disabled"
    elif not clip_dependency_ready:
        clip_state = "missing-local-ai-pack"
    elif clip_status.get("model_ready"):
        clip_state = "ready"
    elif settings.offline_mode:
        clip_state = "local-cache-required"
    else:
        clip_state = "online-preparation-required"
    disabled_features: list[str] = []
    if settings.offline_mode:
        disabled_features.extend(
            [
                "Online reverse geocoding is blocked; local GeoNames/Natural Earth data is allowed.",
                "Caption generation is disabled.",
                "Automatic model downloads are blocked.",
            ]
        )

    payload = {
        "offline_mode": settings.offline_mode,
        "runtime_mode": "offline-local-only" if settings.offline_mode else "standard",
        "deployment_mode": "docker" if _is_docker_runtime() else "native",
        "deployment_label": _deployment_label(),
        "outbound_network_enabled": not settings.offline_mode,
        "disabled_features": disabled_features,
        "local_dependencies": [
            {
                "name": "ffmpeg",
                "state": "optional-legacy-tool" if which("ffmpeg") else "unused",
                "detail": "Legacy video support only; current library sync ignores videos.",
            },
            {
                "name": "ffprobe",
                "state": "optional-legacy-tool" if which("ffprobe") else "unused",
                "detail": "Legacy video support only; current library sync ignores videos.",
            },
            {
                "name": "sips",
                "state": "ready" if which("sips") else "optional-fallback-missing",
                "detail": "macOS image decode/thumbnail fallback.",
            },
            {
                "name": "CLIP semantic embedding",
                "state": clip_state,
                "detail": (
                    f"{settings.semantic_clip_model_name}/{settings.semantic_clip_pretrained}; "
                    "base app works without this optional local AI pack."
                ),
                "dependencies": clip_dependencies,
                "cache": clip_status.get("cache") or {},
            },
            {
                "name": "Face analysis models",
                "state": "ready" if face_models_ready else "missing-local-models",
                "detail": str(detector_path.parent),
            },
            {
                "name": "Local geocoding data",
                "state": "ready" if _local_geodata_ready(settings.geodata_root) else "missing-local-data",
                "detail": "GeoNames + Natural Earth extracts for offline place names.",
            },
            {
                "name": "Caption provider",
                "state": "disabled" if settings.offline_mode else "optional",
                "detail": "Moondream captioning is blocked in offline-local-only mode.",
            },
        ],
    }
    _SECURITY_CACHE = (cache_key, now, payload)
    return deepcopy(payload)


def _local_geodata_ready(root: Path) -> bool:
    return (
        (root / "geonames" / "cities1000.txt").is_file()
        or (root / "geonames" / "cities15000.txt").is_file()
    ) and (root / "naturalearth" / "countries.geojson").is_file()


def _ai_offline_setup_body(
    online_cmd: str,
    offline_cmd: str,
    *,
    error: str | None = None,
) -> str:
    error_html = (
        f'<span class="status-warn">마지막 시도: {escape(error)}</span>'
        if error
        else ""
    )
    return f"""{error_html}
<button class="btn-primary" id="ai-download-btn" onclick="aiPackPrepare(true)">로컬 캐시 확인</button>
<p class="ai-step-desc">이 버튼은 새 모델을 받지 않습니다. 현재 캐시 폴더에 이미 있는 모델을 불러오기만 시도합니다.</p>
<p class="ai-step-desc">캐시가 비어 있으면 한 번만 온라인 준비 모드로 재시작해서 모델을 받은 뒤, 다시 오프라인 모드로 돌아와야 합니다.</p>
<div class="ai-cmd-row">
  <code id="online-ai-cmd">{escape(online_cmd)}</code>
          <button class="btn-copy" onclick="copyText('online-ai-cmd', this)">복사</button>
</div>
<div class="ai-cmd-row">
  <code id="offline-ai-cmd">{escape(offline_cmd)}</code>
  <button class="btn-copy" onclick="copyText('offline-ai-cmd', this)">복사</button>
</div>"""


def _ai_step2_body(
    stage: str,
    error: str | None,
    *,
    offline_mode: bool,
    online_cmd: str,
    offline_cmd: str,
) -> str:
    if stage == "ready":
        return '<span class="status-ok">준비됨</span>'
    if stage == "needs_packages":
        return '<span class="muted">AI 패키지 준비 필요</span>'
    if stage == "downloading":
        return '<span class="ai-spinner"></span><span id="ai-dl-label"> 준비 중...</span>'
    if stage == "error":
        if offline_mode:
            return _ai_offline_setup_body(online_cmd, offline_cmd, error=error)
        retry = '<button class="btn-sm" onclick="aiPackPrepare()">다시 시도</button>'
        return f'<span class="status-warn">오류: {escape(error or "unknown")}</span>{retry}'
    if offline_mode:
        return _ai_offline_setup_body(online_cmd, offline_cmd)
    # needs_download
    return '<button class="btn-primary" id="ai-download-btn" onclick="aiPackPrepare()">모델 받기</button>'


def _ai_step3_body(stage: str, clip_enabled: bool, *, activate_cmd: str) -> str:
    if stage != "ready":
        return '<span class="muted">모델 준비 필요</span>'
    if clip_enabled:
        return '<span class="status-ok">활성화됨</span>'
    return '''<p class="ai-step-desc">아래 환경 변수를 설정하고 서버를 재시작하세요.</p>
<div class="ai-cmd-row">
  <code id="activate-cmd">''' + escape(activate_cmd) + '''</code>
  <button class="btn-copy" onclick="copyText('activate-cmd', this)">복사</button>
</div>'''


def _clip_runtime_commands(settings: AppSettings) -> tuple[str, str, str]:
    model_root = settings.model_root.expanduser().resolve()
    hf_home = model_root / "hf"
    torch_home = model_root / "torch"
    if _is_docker_runtime():
        online_cmd = "PHOTOME_OFFLINE_MODE=0 docker compose --env-file .env.docker.example up -d photome"
        offline_cmd = "PHOTOME_OFFLINE_MODE=1 docker compose --env-file .env.docker.example up -d photome"
        activate_cmd = "PHOTOME_OFFLINE_MODE=1 PHOTOME_CLIP_ENABLED=1 docker compose --env-file .env.docker.example up -d photome"
        return online_cmd, offline_cmd, activate_cmd

    env_prefix = " ".join(
        [
            f"HF_HOME={shlex.quote(str(hf_home))}",
            f"TORCH_HOME={shlex.quote(str(torch_home))}",
            "PHOTOME_CLIP_ENABLED=1",
        ]
    )
    online_cmd = f"{env_prefix} PHOTOME_OFFLINE_MODE=0 python -m app.main"
    offline_cmd = f"{env_prefix} PHOTOME_OFFLINE_MODE=1 python -m app.main"
    activate_cmd = offline_cmd
    return online_cmd, offline_cmd, activate_cmd


def _render_people_manager(people: list[dict[str, Any]]) -> str:
    if not people:
        return """
        <div class="empty-panel">
          5회 이상 감지된 얼굴 그룹이 아직 없습니다. 이름과 애칭 매핑은 반복해서 나온 얼굴만 표시합니다.
        </div>
        """
    rows = []
    for person in people:
        alias_list = [str(a) for a in person.get("aliases", []) if a]
        aliases_csv = ", ".join(alias_list)
        has_aliases = bool(alias_list)
        display_name = str(person.get("display_name") or "")
        search_text = " ".join(
            [
                display_name,
                aliases_csv,
                f"person-{int(person['id']):06d}",
            ]
        ).lower()
        face_samples = "".join(
            f"""
            <button class="face-sample person-preview-trigger" type="button" data-person-id="{int(person['id'])}" data-person-label="{escape(str(person['display_name']))}" title="{escape(str(sample.get('filename') or 'sample'))}">
              <img class="face-sample-img" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" data-src="/people/faces/{int(sample['face_id'])}/crop" alt="{escape(str(person['display_name']))} face sample" loading="lazy" decoding="async" fetchpriority="low">
            </button>
            """
            for sample in person.get("sample_faces", [])[:1]
            if sample.get("face_id") is not None
        ) or '<span class="face-empty">No face preview</span>'
        alias_chips = "".join(
            f'<span class="alias-chip">{escape(a)}<button type="button" class="alias-remove" data-alias="{escape(a)}" title="{escape(a)} 제거">×</button></span>'
            for a in alias_list
        ) or '<span class="alias-chips-empty-hint">병합 시 여기에 표시</span>'
        is_unnamed = display_name.startswith("person-") and not has_aliases
        row_title = "이름 없음" if is_unnamed else display_name
        row_hint = (
            "대표 이름을 비워 두고 애칭만 적으면 첫 애칭이 대표 이름으로 저장됩니다."
            if is_unnamed
            else "검색에 쓸 이름과 애칭을 함께 관리합니다."
        )
        name_placeholder = "대표 이름 입력" if is_unnamed else "이름 입력"
        save_label = "이름 저장" if is_unnamed else "저장"
        status_badge = (
            '<span class="person-status-badge unnamed">이름 필요</span>'
            if is_unnamed
            else '<span class="person-status-badge named">이름 있음</span>'
        )
        rows.append(
            f"""
            <form class="person-row{'  has-aliases' if has_aliases else ''}{'  unnamed' if is_unnamed else ''}" data-person-id="{int(person['id'])}" data-person-label="{escape(str(person['display_name']))}" data-person-search="{escape(search_text)}" data-media-count="{int(person.get('media_count') or 0)}" data-face-count="{int(person.get('face_count') or 0)}" data-named="{0 if is_unnamed else 1}">
              <label class="person-select" title="병합할 인물 선택">
                <input type="checkbox" class="person-merge-checkbox" aria-label="Select {escape(str(person['display_name']))} for merge">
              </label>
              <div class="face-samples">{face_samples}</div>
              <div class="person-meta">
                <div class="person-title-row">
                  <strong class="person-title">{escape(row_title)}</strong>
                  {status_badge}
                </div>
                <div class="person-metrics">
                  <span class="person-photo-count">{int(person.get('media_count') or 0)}장</span>
                  <span class="person-face-count">얼굴 {int(person.get('face_count') or 0)}회</span>
                  <small class="person-id-hint" title="내부 ID">person-{int(person['id']):06d}</small>
                </div>
                <small class="person-row-hint">{escape(row_hint)}</small>
              </div>
              <input name="display_name" value="{escape('' if is_unnamed else display_name)}" placeholder="{name_placeholder}">
              <div class="alias-editor">
                <input class="person-alias-input" name="aliases" value="{escape(aliases_csv)}" placeholder="애칭 입력, 쉼표로 구분">
                <div class="alias-chips-container{' empty' if not has_aliases else ''}" title="저장된 애칭/병합된 다른 이름들">{alias_chips}</div>
              </div>
              <button class="btn-copy person-preview-trigger" type="button" data-person-id="{int(person['id'])}" data-person-label="{escape(str(person['display_name']))}">사진 보기</button>
              <button class="btn-sm" type="submit">{save_label}</button>
              <small class="person-save-state" aria-live="polite"></small>
            </form>
            """
        )
    return "".join(rows)


def _people_manager_summary(total_count: int, named_count: int) -> str:
    count = total_count
    named = named_count
    if count == 0:
        return "5회 이상 0명"
    return f"{count}명 · 이름 지정 {named}명"


def _catalog_breakdown(status_counts: dict[str, int]) -> dict[str, Any]:
    counts = {str(status): int(count or 0) for status, count in (status_counts or {}).items()}
    hidden_statuses = {"excluded", "missing", "replaced"}
    not_applicable = sum(counts.get(status, 0) for status in hidden_statuses)
    total = sum(count for status, count in counts.items() if status not in hidden_statuses)
    completed = counts.get("thumb_done", 0) + counts.get("analysis_done", 0)
    scheduled = counts.get("metadata_done", 0) + counts.get("active", 0)
    error = counts.get("error", 0)
    known = {"thumb_done", "analysis_done", "metadata_done", "active", "missing", "replaced", "error", "excluded"}
    other = sum(count for status, count in counts.items() if status not in known)
    scheduled += other
    return {
        "total": total,
        "completed": completed,
        "scheduled": scheduled,
        "not_applicable": not_applicable,
        "error": error,
        "summary_text": (
            f"1. 토탈 {total}개 · 2. 완료 {completed}개 · 3. 예정 {scheduled}개 · 4. 미해당 {not_applicable}개 · 5. 오류 {error}개"
        ),
        "notes": [
            "토탈: 현재 처리 대상 사진만 집계합니다. 미해당(missing/replaced/excluded)은 별도로 분리합니다.",
            "완료: 썸네일 또는 분석까지 끝나서 사진첩에서 바로 쓸 수 있는 파일입니다.",
            "예정: 스캔/메타데이터까지만 끝나서 다음 처리(썸네일·기본 분석)가 남아 있는 파일입니다.",
            "미해당: 원본이 현재 없거나(missing), 다른 파일로 대체됐거나(replaced), 현재 제품 범위에서 제외된 파일(excluded)입니다.",
            "오류: 지난 파일 처리에서 실패해서 '오류 항목만 재처리' 대상이 된 파일입니다.",
        ],
    }


def _ai_summary(
    *,
    eligible_media: int,
    clip_embeddings: int,
    completed_images: int,
    completed_videos: int,
    total_images: int,
    semantic_job_errors: int,
) -> dict[str, Any]:
    remaining_clip = max(0, eligible_media - clip_embeddings)
    not_applicable = max(0, total_images - eligible_media)
    completed_without_ai = max(0, completed_images - clip_embeddings)
    error = max(0, semantic_job_errors)
    notes = [
        "이미지 AI는 사진 검색·자동분류에 쓰는 CLIP 임베딩 상태를 뜻합니다.",
        "이미지 AI 완료 수는 현재 CLIP 임베딩 버전까지 생성된 사진만 셉니다.",
        "파일 완료는 사진첩에서 볼 수 있는 상태이고, 이미지 AI 완료는 CLIP 임베딩까지 준비된 상태라 두 숫자는 다를 수 있습니다.",
    ]
    if completed_without_ai:
        notes.append(
            f"완료 사진 중 {completed_without_ai}개는 썸네일/기본 처리는 끝났지만 이미지 AI 임베딩은 아직 남아 있습니다."
        )
    if not_applicable:
        notes.append(f"{not_applicable}개는 아직 파일 처리가 끝나지 않았거나 원본 없음/제외 상태라 이미지 AI 대상에서 빠져 있습니다.")
    if error:
        notes.append(f"이미지 AI 작업 오류 기록이 {error}개 있습니다. 파일 오류와 별도로 semantic maintenance 작업 실패를 뜻합니다.")
    if remaining_clip:
        notes.append(f"현재 이미지 AI 대상 {eligible_media}개 중 {clip_embeddings}개만 완료됐고, {remaining_clip}개가 남아 있습니다.")
    else:
        notes.append("현재 이미지 AI 대상 사진은 모두 임베딩까지 완료된 상태입니다.")
    return {
        "eligible_media": eligible_media,
        "clip_embeddings": clip_embeddings,
        "completed_images": completed_images,
        "completed_videos": completed_videos,
        "remaining_clip": remaining_clip,
        "not_applicable": not_applicable,
        "error": error,
        "summary_text": (
            f"1. 대상 {eligible_media}개 · 2. 완료 {clip_embeddings}개 · 3. 예정 {remaining_clip}개 · "
            f"4. 미해당 {not_applicable}개 · 5. 오류 {error}개"
        ),
        "note_text": f"대상 {eligible_media}개 · 완료 {clip_embeddings}개 · 예정 {remaining_clip}개",
        "notes": notes,
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    payload = await status(request)
    settings: AppSettings = require_state(request, "settings")
    scheduler = payload["scheduler"]
    semantic = payload["semantic"]
    performance = payload["performance"]
    resource_settings = performance.get("resource_settings") or {}
    catalog = payload["catalog"]
    jobs = payload["jobs"]
    health = payload["health"]
    security = payload["security"]
    source_roots = payload["storage"]["source_roots"]
    known_source_roots = payload["storage"].get("known_source_roots") or []
    display_source_roots = _preferred_input_source_roots(settings, source_roots, known_source_roots)
    display_known_source_roots = [_display_source_root_path(settings, path) for path in known_source_roots]
    source_roots_text = escape("\n".join(display_source_roots))
    known_source_roots_html = (
        "<br>".join(escape(path) for path in display_known_source_roots)
        if display_known_source_roots
        else '<span class="muted">No cataloged source roots yet</span>'
    )
    source_root_guidance = _source_root_guidance(settings)
    active_library_job_json = json.dumps(jobs.get("active_library_job"), default=str)
    active_job = jobs.get("active_library_job")
    active_kind = str((active_job or {}).get("job_kind") or "")
    active_status = str((active_job or {}).get("status") or "")
    has_active_job = active_status in {"queued", "running"}
    phase2_active = has_active_job and active_kind in {"semantic_backfill", "semantic_maintenance"}
    phase1_active = has_active_job and not phase2_active
    phase1_card_class = "card scan-card is-running" if phase1_active else "card scan-card"
    phase2_card_class = "card scan-card is-running" if phase2_active else "card scan-card"
    phase1_state_text = "RUNNING" if phase1_active else "IDLE"
    phase2_state_text = "RUNNING" if phase2_active else ("WAITING" if phase1_active else "IDLE")
    phase1_state_class = "status-running" if phase1_active else "status-idle"
    phase2_state_class = "status-running" if phase2_active else ("status-warn" if phase1_active else "status-idle")
    phase1_scan_disabled = " disabled" if has_active_job else ""
    phase2_run_disabled = " disabled" if (phase1_active or phase2_active) else ""
    phase2_cancel_display = "" if phase2_active else "display:none"
    phase1_live_class = "live-panel is-running" if phase1_active else "live-panel"
    phase2_live_class = "live-panel is-running" if phase2_active else ("live-panel is-waiting" if phase1_active else "live-panel")
    phase1_state_label = _phase_state_label(phase1_state_text)
    phase2_state_label = _phase_state_label(phase2_state_text)
    phase1_live_text = _dashboard_job_progress(active_job) if phase1_active else "대기 중"
    phase2_live_text = (
        _dashboard_job_progress(active_job)
        if phase2_active
        else ("사진 가져오기가 끝날 때까지 대기 중" if phase1_active else "대기 중")
    )
    phase1_schedule_label = _schedule_label(payload["scheduler"].get("library_interval_hours"))
    phase2_schedule_label = ""  # unified into library schedule
    semantic_coverage = semantic["coverage"]
    scheduler_background_semantic_active = (
        scheduler.get("background_task_kind") in {"semantic_backfill", "semantic_maintenance"}
        and scheduler.get("background_task_state") == "running"
    )
    ai_summary = performance.get("ai_summary") or {}
    ai_pending_count = int(ai_summary.get("remaining_clip") or semantic_coverage.get("remaining_for_clip") or 0)
    if phase2_active or scheduler_background_semantic_active:
        ai_metric_state_label = "진행 중"
        ai_metric_state_class = "metric-state-badge is-running"
        ai_metric_state_detail = str(scheduler.get("background_task_message") or phase2_live_text or "이미지 AI 분석 중")
    elif phase1_active:
        ai_metric_state_label = "대기"
        ai_metric_state_class = "metric-state-badge is-waiting"
        ai_metric_state_detail = "동기화가 끝나면 이미지 AI가 이어서 처리됩니다."
    elif ai_pending_count:
        ai_metric_state_label = "진행 중"
        ai_metric_state_class = "metric-state-badge is-running"
        next_ai_run = scheduler.get("next_semantic_maintenance_at") or "자동 주기"
        ai_metric_state_detail = (
            f"남은 {ai_pending_count}개가 있어 이미지 AI 자동 처리 대상입니다. "
            f"전체 동기화 중이 아니면 백그라운드에서 순차 처리합니다. 다음 확인: {next_ai_run}"
        )
    else:
        ai_metric_state_label = "완료"
        ai_metric_state_class = "metric-state-badge is-idle"
        ai_metric_state_detail = "현재 이미지 AI 대상 사진은 모두 완료됐습니다."
    clip_dependency = next(
        (item for item in security["local_dependencies"] if item["name"] == "CLIP semantic embedding"),
        {"state": "unknown", "detail": "", "dependencies": {}, "cache": {}},
    )
    clip_deps = clip_dependency.get("dependencies") or {}
    clip_cache = clip_dependency.get("cache") or {}
    clip_ready = clip_dependency.get("state") == "ready"
    clip_enabled = clip_dependency.get("state") != "disabled"
    ai_pack = get_ai_pack_state()
    ai_pack_stage = ai_pack["stage"]  # needs_packages | needs_download | downloading | ready | error
    ai_pack_model_name = ai_pack["config"].get("model_name", settings.semantic_clip_model_name)
    ai_pack_pretrained = ai_pack["config"].get("pretrained", settings.semantic_clip_pretrained)
    online_ai_cmd, offline_ai_cmd, activate_ai_cmd = _clip_runtime_commands(settings)
    people = payload.get("people", [])
    people_stats = payload.get("people_stats") or {}
    people_manager_html = _render_people_manager(people)
    people_manager_summary = _people_manager_summary(
        int(people_stats.get("total") or len(people)),
        int(people_stats.get("named") or 0),
    )
    people_json = json.dumps([{"id": p["id"], "display_name": p["display_name"]} for p in people])

    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>photome dashboard</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f5f5f7;
      --paper: rgba(255,255,255,0.90);
      --panel: #ffffff;
      --line: rgba(0,0,0,0.10);
      --text: #1d1d1f;
      --muted: #86868b;
      --accent: #0a84ff;
      --accent-soft: rgba(10,132,255,0.12);
      --ok: #248a3d;
      --warn: #b25000;
      --shadow: 0 1px 3px rgba(0,0,0,0.06);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #1c1c1e;
        --paper: rgba(44,44,46,0.90);
        --panel: #2c2c2e;
        --line: rgba(255,255,255,0.12);
        --text: #f5f5f7;
        --muted: #98989d;
        --accent: #0a84ff;
        --accent-soft: rgba(10,132,255,0.24);
        --ok: #30d158;
        --warn: #ff9f0a;
        --shadow: 0 1px 3px rgba(0,0,0,0.4);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .shell {{
      width: min(1280px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 20px 0 48px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 16px;
      margin-bottom: 18px;
      padding: 24px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(1.8rem, 3vw, 2.6rem);
      line-height: 1.05;
      letter-spacing: -0.02em;
      font-family: inherit;
    }}
    .eyebrow {{
      display: inline-flex;
      margin-bottom: 12px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(19,32,42,0.06);
      color: var(--accent);
      font-size: .74rem;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
      max-width: 64ch;
    }}
    .hero-links {{
      display: flex;
      gap: 10px;
      margin-top: 16px;
      flex-wrap: wrap;
    }}
    .link-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 10px 16px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      text-decoration: none;
      font-weight: 600;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 16px;
    }}
    .card {{
      grid-column: span 6;
      padding: 18px;
      border-radius: 24px;
      border: 1px solid var(--line);
      background: var(--panel);
      box-shadow: 0 10px 26px rgba(19,32,42,0.06);
    }}
    .card.full {{ grid-column: 1 / -1; }}
    .admin-only {{ display: none; }}
    .card h2 {{
      margin: 0 0 10px;
      font-size: 1.05rem;
      letter-spacing: -0.02em;
    }}
    .sub {{
      margin: 0 0 14px;
      color: var(--muted);
      font-size: .92rem;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .metric {{
      padding: 12px 13px;
      border-radius: 18px;
      background: rgba(19,32,42,0.04);
      border: 1px solid rgba(19,32,42,0.06);
    }}
    .metric.clickable {{
      cursor: pointer;
      transition: background 0.15s, box-shadow 0.15s;
    }}
    .metric.clickable:hover {{
      background: rgba(19,32,42,0.08);
      box-shadow: 0 2px 10px rgba(0,0,0,0.08);
    }}
    .metric strong {{
      display: block;
      margin-top: 4px;
      font-size: 1.05rem;
      letter-spacing: -0.02em;
    }}
    .metric small {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: .76rem;
      line-height: 1.35;
    }}
    .metric-label-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .metric-state-badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 7px;
      border-radius: 999px;
      background: rgba(19,32,42,0.05);
      color: var(--muted);
      border: 1px solid rgba(19,32,42,0.08);
      font-size: .68rem;
      font-weight: 900;
      white-space: nowrap;
    }}
    .metric-state-badge::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
    }}
    .metric-state-badge.is-running {{
      color: var(--accent);
      background: var(--accent-soft);
      border-color: rgba(204,95,50,0.26);
    }}
    .metric-state-badge.is-running::before {{ animation: pulse 1.1s infinite; }}
    .metric-state-badge.is-waiting {{
      color: var(--warn);
      background: rgba(180,106,21,0.08);
      border-color: rgba(180,106,21,0.18);
    }}
    .metric-state-badge.is-idle {{ color: var(--ok); }}
    .metric-status-note {{ color: var(--muted); }}
    .detail-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.45);
      z-index: 300;
      align-items: center;
      justify-content: center;
    }}
    .detail-overlay.open {{ display: flex; }}
    .detail-dialog {{
      background: #fff;
      border-radius: 20px;
      width: min(480px, 92vw);
      max-height: 72vh;
      display: flex;
      flex-direction: column;
      box-shadow: 0 24px 64px rgba(0,0,0,0.28);
      overflow: hidden;
    }}
    .detail-header {{
      padding: 16px 20px 12px;
      border-bottom: 1px solid rgba(0,0,0,0.07);
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }}
    .detail-header h3 {{ margin: 0; font-size: .95rem; }}
    .detail-close {{
      background: rgba(0,0,0,0.07);
      border: none;
      cursor: pointer;
      width: 26px; height: 26px;
      border-radius: 50%;
      font-size: .9rem;
      display: flex; align-items: center; justify-content: center;
      color: var(--muted);
    }}
    .detail-close:hover {{ background: rgba(0,0,0,0.13); }}
    .detail-body {{
      overflow-y: auto;
      padding: 8px 6px 14px;
      flex: 1;
    }}
    .detail-loading {{ padding: 24px 20px; text-align: center; color: var(--muted); font-size: .85rem; }}
    .detail-item {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      padding: 5px 14px;
      border-radius: 8px;
    }}
    .detail-item:hover {{ background: rgba(0,0,0,0.04); }}
    .detail-item-label {{ font-size: .86rem; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .detail-item-sub {{ font-size: .72rem; color: var(--muted); margin-left: 6px; flex-shrink: 0; }}
    .detail-item-count {{ font-size: .82rem; font-weight: 600; color: var(--muted); white-space: nowrap; margin-left: 10px; flex-shrink: 0; }}
    .detail-section-head {{ font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); padding: 10px 14px 3px; }}
    .detail-summary-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
      padding: 8px 10px 12px;
    }}
    .detail-summary-card {{
      border: 1px solid rgba(19,32,42,0.08);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(19,32,42,0.03);
    }}
    .detail-summary-card strong {{
      display: block;
      font-size: .92rem;
      margin-bottom: 3px;
    }}
    .detail-summary-card small {{
      color: var(--muted);
      font-size: .74rem;
      line-height: 1.35;
    }}
    .detail-notes {{
      margin: 0 10px 10px;
      padding: 10px 12px 10px 28px;
      border-radius: 12px;
      background: rgba(204,95,50,0.08);
      color: var(--text);
      font-size: .8rem;
      line-height: 1.45;
    }}
    .detail-notes li + li {{ margin-top: 6px; }}
    .pill-row {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(19,32,42,0.05);
      border: 1px solid rgba(19,32,42,0.07);
      font-size: .88rem;
    }}
    .pill-button {{
      cursor: pointer;
      color: var(--text);
      font: inherit;
    }}
    .status-ok {{ color: var(--ok); }}
    .status-warn {{ color: var(--warn); }}
    .status-idle {{ color: var(--muted); }}
    .status-running {{
      color: var(--accent);
      font-weight: 900;
      letter-spacing: .08em;
    }}
    .run-badge {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(19,32,42,0.05);
      border: 1px solid rgba(19,32,42,0.08);
      font-size: .78rem;
      font-weight: 900;
      letter-spacing: .08em;
    }}
    .run-badge.is-running {{
      color: var(--accent);
      background: var(--accent-soft);
      border-color: rgba(204,95,50,0.28);
      box-shadow: 0 8px 22px rgba(204,95,50,0.12);
    }}
    .run-badge.is-running::before {{
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 5px rgba(204,95,50,0.12);
    }}
    .nas-badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      padding: 2px 8px;
      border-radius: 20px;
      border: 1.5px solid transparent;
      vertical-align: middle;
    }}
    .nas-badge::before {{
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      flex-shrink: 0;
    }}
    .nas-ok {{ color: #1a7a55; background: #e6f5ef; border-color: #a3d9c0; }}
    .nas-ok::before {{ background: #2aab72; }}
    .nas-warn {{ color: #8a6000; background: #fff8e0; border-color: #f0d060; }}
    .nas-warn::before {{ background: #e0a800; }}
    .nas-error {{ color: #b00020; background: #fdecea; border-color: #f5a0a0; }}
    .nas-error::before {{ background: #d32f2f; animation: pulse 1.2s infinite; }}
    .nas-unknown {{ color: var(--muted); background: var(--bg); border-color: var(--line); }}
    .nas-unknown::before {{ background: var(--muted); }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }}
    }}
    .live-panel {{
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(19,32,42,0.04);
      border: 1px solid rgba(19,32,42,0.07);
      color: var(--muted);
      font: .9rem "Inter", "Helvetica Neue", sans-serif;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}
    .live-panel.is-running {{
      color: var(--text);
      background: rgba(204,95,50,0.10);
      border-color: rgba(204,95,50,0.24);
    }}
    .live-panel.is-waiting {{
      color: var(--warn);
      background: rgba(180,106,21,0.08);
      border-color: rgba(180,106,21,0.18);
    }}
    .list {{
      display: grid;
      gap: 10px;
    }}
    .compact-list {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .mini-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 16px;
      background: rgba(19,32,42,0.04);
      font-size: .92rem;
    }}
    .row span:last-child {{
      overflow-wrap: anywhere;
      text-align: right;
    }}
    .advanced-disclosure {{
      padding: 0;
    }}
    .advanced-disclosure summary {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 18px;
      cursor: pointer;
      list-style: none;
    }}
    .advanced-disclosure summary::-webkit-details-marker {{ display: none; }}
    .advanced-body {{
      display: grid;
      gap: 16px;
      padding: 0 18px 18px;
    }}
    .advanced-body .card {{
      grid-column: 1 / -1;
      box-shadow: none;
    }}
    .scan-form {{
      display: grid;
      gap: 10px;
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
    }}
    .scan-form label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: .86rem;
      font-weight: 700;
    }}
    .scan-form textarea {{
      min-height: 86px;
      resize: vertical;
      padding: 11px 12px;
      border: 1px solid rgba(19,32,42,0.14);
      border-radius: 14px;
      background: rgba(255,255,255,0.9);
      color: var(--text);
      font: .86rem "SFMono-Regular", "Menlo", monospace;
      line-height: 1.45;
    }}
    .scan-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .scan-actions label {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--text);
      font-size: .9rem;
      font-weight: 600;
    }}
    .scan-actions select,
    .scan-actions input[type="number"] {{
      min-height: 36px;
      padding: 7px 10px;
      border: 1px solid rgba(19,32,42,0.14);
      border-radius: 12px;
      background: rgba(255,255,255,0.9);
      color: var(--text);
      font: .88rem "Inter", "Helvetica Neue", sans-serif;
    }}
    .scan-actions button {{
      min-height: 40px;
      padding: 9px 14px;
      border: 0;
      border-radius: 999px;
      background: var(--accent);
      color: white;
      font-weight: 800;
      cursor: pointer;
    }}
    .scan-actions button:disabled {{
      opacity: .62;
      cursor: progress;
    }}
    .field-help {{
      color: var(--muted);
      font-size: .78rem;
      font-weight: 500;
      line-height: 1.4;
    }}
    .source-picker {{
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(19,32,42,0.035);
    }}
    .source-picker[hidden] {{ display: none; }}
    .source-picker-bar {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .source-picker-path {{
      flex: 1;
      min-width: 220px;
      padding: 8px 10px;
      border-radius: 10px;
      background: rgba(255,255,255,0.75);
      border: 1px solid rgba(19,32,42,0.08);
      font: .78rem "SFMono-Regular", "Menlo", monospace;
      overflow-wrap: anywhere;
    }}
    .source-picker-list {{
      display: grid;
      gap: 6px;
      max-height: 260px;
      overflow: auto;
    }}
    .source-picker-item {{
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 8px;
      align-items: center;
      min-height: 38px;
      padding: 8px 10px;
      border: 1px solid rgba(19,32,42,0.08);
      border-radius: 10px;
      background: rgba(255,255,255,0.72);
      text-align: left;
    }}
    .source-picker-item strong {{
      overflow-wrap: anywhere;
      font-size: .86rem;
    }}
    .source-picker-item small {{
      color: var(--muted);
      font: .72rem "SFMono-Regular", "Menlo", monospace;
      overflow-wrap: anywhere;
    }}
    .source-picker-item button,
    .source-picker-bar button {{
      min-height: 30px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.88);
      color: var(--text);
      cursor: pointer;
      font-weight: 800;
    }}
    .source-picker-item button.primary {{
      border-color: rgba(204,95,50,0.32);
      background: var(--accent);
      color: white;
    }}
    .scan-card.is-running {{
      border-color: rgba(204,95,50,0.32);
      box-shadow: 0 18px 45px rgba(204,95,50,0.12);
    }}
    .scan-card.is-running .scan-title::after {{
      content: "";
      display: inline-block;
      width: 14px;
      height: 14px;
      margin-left: 8px;
      border-radius: 50%;
      border: 2px solid rgba(204,95,50,0.22);
      border-top-color: var(--accent);
      vertical-align: -2px;
      animation: spin 850ms linear infinite;
    }}
    .live-status {{
      color: var(--muted);
      font-family: "SFMono-Regular", "Menlo", monospace;
      font-size: .78rem;
      text-align: right;
    }}
    .scan-card.is-running .live-status {{
      color: var(--accent);
      font-weight: 800;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .scan-result {{
      display: none;
      margin: 0;
      padding: 10px 12px;
      overflow: auto;
      border-radius: 14px;
      background: rgba(19,32,42,0.05);
      color: var(--text);
      font: .8rem "SFMono-Regular", "Menlo", monospace;
      line-height: 1.45;
      white-space: pre-wrap;
    }}
    .scan-result.visible {{ display: block; }}
    /* AI pack setup steps */
    .ai-steps {{ display: flex; flex-direction: column; gap: 16px; margin-top: 16px; }}
    .ai-step {{ display: flex; gap: 14px; align-items: flex-start; padding: 14px 16px; border-radius: 10px; background: var(--bg); border: 1.5px solid var(--line); }}
    .ai-step-done {{ opacity: 0.55; }}
    .ai-step-active {{ border-color: var(--accent); background: var(--accent-soft); }}
    .ai-step-locked {{ opacity: 0.35; pointer-events: none; }}
    .ai-step-num {{ width: 26px; height: 26px; border-radius: 50%; background: var(--accent); color: #fff; font-size: .8rem; font-weight: 700; display: flex; align-items: center; justify-content: center; flex-shrink: 0; margin-top: 1px; }}
    .ai-step-done .ai-step-num {{ background: var(--ok); }}
    .ai-step-locked .ai-step-num {{ background: var(--muted); }}
    .ai-step-body {{ flex: 1; display: flex; flex-direction: column; gap: 6px; }}
    .ai-step-body strong {{ font-size: .95rem; }}
    .ai-step-desc {{ margin: 0; font-size: .85rem; color: var(--muted); }}
    .ai-cmd-row {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; }}
    .ai-cmd-row code {{ flex: 1; background: var(--paper); border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; font-size: .85rem; }}
    .btn-primary {{ padding: 7px 18px; background: var(--accent); color: #fff; border: none; border-radius: 8px; font-size: .875rem; font-weight: 600; cursor: pointer; }}
    .btn-primary:hover {{ opacity: .88; }}
    .btn-primary:disabled {{ opacity: .4; cursor: default; }}
    .btn-sm {{ padding: 4px 12px; background: var(--accent); color: #fff; border: none; border-radius: 6px; font-size: .8rem; cursor: pointer; }}
    .btn-copy {{ padding: 4px 10px; background: transparent; border: 1px solid var(--line); border-radius: 6px; font-size: .78rem; cursor: pointer; color: var(--muted); }}
    .btn-copy:hover {{ border-color: var(--accent); color: var(--accent); }}
    .manager-disclosure {{
      padding: 0;
    }}
    .manager-disclosure summary {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 20px;
      cursor: pointer;
      list-style: none;
    }}
    .manager-disclosure summary::-webkit-details-marker {{ display: none; }}
    .manager-disclosure summary h2 {{
      margin: 0 0 6px;
    }}
    .manager-disclosure .sub {{
      margin: 0;
    }}
    .disclosure-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(255,255,255,0.7);
      font-size: .82rem;
      font-weight: 800;
      white-space: nowrap;
    }}
    .manager-disclosure[open] .disclosure-pill {{
      color: var(--accent);
      border-color: rgba(204,95,50,0.28);
      background: var(--accent-soft);
    }}
    .manager-body {{
      padding: 0 20px 20px;
    }}
    .people-merge-bar {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin: 12px 0 10px;
      padding: 10px;
      border: 1px solid rgba(204,95,50,0.22);
      border-radius: 8px;
      background: var(--accent-soft);
      font-size: .86rem;
    }}
    .people-merge-bar[hidden] {{ display: none; }}
    .people-merge-bar label {{ display: inline-flex; align-items: center; gap: 6px; font-weight: 800; }}
    .people-merge-bar select {{
      min-height: 32px;
      padding: 4px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255,255,255,0.92);
    }}
    .people-merge-bar.is-merging {{
      border-color: rgba(204,95,50,0.46);
      box-shadow: 0 0 0 1px rgba(204,95,50,0.12) inset;
    }}
    .people-merge-bar.is-merging #people-merge-state::before {{
      content: "";
      display: inline-block;
      width: 10px;
      height: 10px;
      margin-right: 6px;
      border: 2px solid rgba(204,95,50,0.28);
      border-top-color: var(--accent);
      border-radius: 50%;
      vertical-align: -1px;
      animation: spin 0.85s linear infinite;
    }}
    .people-merge-bar.is-merging ~ .person-list {{
      opacity: 0.62;
      pointer-events: none;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .people-tools {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto;
      gap: 8px;
      align-items: center;
      margin: 12px 0 10px;
    }}
    .people-queue-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 8px 0 0;
    }}
    .people-queue-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.7);
      font-size: .8rem;
      color: var(--muted);
    }}
    .people-mode-switch {{
      display: inline-flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 8px 0 2px;
    }}
    .people-mode-btn {{
      min-height: 34px;
      padding: 6px 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255,255,255,0.8);
      color: var(--text);
      font: .82rem "Inter", "Helvetica Neue", sans-serif;
      cursor: pointer;
    }}
    .people-mode-btn.active {{
      border-color: rgba(204,95,50,0.4);
      background: rgba(204,95,50,0.12);
      color: var(--accent);
      font-weight: 800;
    }}
    .people-more-row {{
      display: flex;
      justify-content: center;
      margin-top: 12px;
    }}
    .people-tools input,
    .people-tools select,
    .person-preview-tools input {{
      min-height: 36px;
      padding: 7px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,0.92);
      color: var(--text);
      font: .84rem "Inter", "Helvetica Neue", sans-serif;
    }}
    .people-tools small {{
      color: var(--muted);
      font-weight: 800;
      white-space: nowrap;
    }}
    .person-list {{ display: grid; gap: 8px; margin-top: 14px; }}
    .person-row {{
      display: grid;
      grid-template-columns: auto minmax(150px, .72fr) minmax(200px, 1.1fr) minmax(170px, .85fr) minmax(220px, 1.35fr) auto auto minmax(72px, .35fr);
      gap: 8px;
      align-items: start;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255,255,255,0.58);
    }}
    .person-row.has-aliases {{
      border-left: 3px solid rgba(59,130,246,0.45);
    }}
    .person-row.unnamed {{
      border-left: 3px solid rgba(204,95,50,0.45);
      background: rgba(255,250,246,0.86);
    }}
    .person-meta {{
      display: grid;
      gap: 4px;
      min-width: 0;
    }}
    .person-title-row {{
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      flex-wrap: wrap;
    }}
    .person-title {{
      font-size: .92rem;
      line-height: 1.2;
    }}
    .person-status-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      font-size: .73rem;
      font-weight: 700;
      border: 1px solid transparent;
    }}
    .person-status-badge.unnamed {{
      color: var(--accent);
      background: rgba(204,95,50,0.12);
      border-color: rgba(204,95,50,0.22);
    }}
    .person-status-badge.named {{
      color: var(--ok);
      background: rgba(47,143,91,0.10);
      border-color: rgba(47,143,91,0.18);
    }}
    .person-metrics {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: .78rem;
    }}
    .person-face-count {{
      font-weight: 600;
    }}
    .person-row-hint {{
      color: var(--muted);
      line-height: 1.35;
    }}
    .alias-editor {{
      display: grid;
      gap: 6px;
      min-width: 0;
    }}
    .alias-chips-container {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      min-height: 38px;
      align-items: center;
      padding: 4px 6px;
      border: 1px solid rgba(19,32,42,0.14);
      border-radius: 8px;
      background: rgba(255,255,255,0.92);
    }}
    .alias-chips-container.empty {{
      border-style: dashed;
      background: rgba(255,255,255,0.5);
    }}
    .alias-chip {{
      display: inline-flex;
      align-items: center;
      gap: 2px;
      background: rgba(59,130,246,0.1);
      border: 1px solid rgba(59,130,246,0.28);
      border-radius: 10px;
      padding: 2px 6px 2px 9px;
      font-size: .76rem;
      color: var(--text);
      white-space: nowrap;
    }}
    .alias-remove {{
      background: none;
      border: none;
      cursor: pointer;
      color: rgba(19,32,42,0.38);
      font-size: .8rem;
      padding: 0 2px;
      line-height: 1;
      border-radius: 50%;
    }}
    .alias-remove:hover {{ color: #c0392b; }}
    .alias-chips-empty-hint {{
      color: var(--muted);
      font-size: .76rem;
    }}
    .person-select {{ display: flex; align-items: center; justify-content: center; }}
    .person-select input {{ width: 18px; height: 18px; accent-color: var(--accent); }}
    .person-row div {{ display: grid; gap: 2px; }}
    .person-row span, .person-row small {{ color: var(--muted); font-size: .78rem; }}
    .person-photo-count {{ color: var(--text) !important; font-size: .88rem !important; font-weight: 600; }}
    .person-id-hint {{ color: var(--muted) !important; font-size: .72rem !important; opacity: 0.55; }}
    .person-row.unnamed {{ background: rgba(19,32,42,0.03); opacity: 0.72; }}
    .person-row.unnamed:hover {{ opacity: 1; }}
    .face-samples {{
      display: flex !important;
      flex-direction: row;
      gap: 6px;
      align-items: center;
    }}
    .face-sample {{
      width: 54px;
      height: 54px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 50%;
      background: rgba(19,32,42,0.08);
      padding: 0;
      cursor: pointer;
    }}
    .face-sample img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .face-empty {{
      padding: 6px 8px;
      border-radius: 999px;
      background: rgba(19,32,42,0.06);
      white-space: nowrap;
    }}
    .person-row input {{
      min-height: 38px;
      padding: 8px 10px;
      border: 1px solid rgba(19,32,42,0.14);
      border-radius: 8px;
      background: rgba(255,255,255,0.92);
      color: var(--text);
      font: .88rem "Inter", "Helvetica Neue", sans-serif;
    }}
    .person-preview-modal {{
      position: fixed;
      inset: 0;
      z-index: 80;
      display: grid;
      place-items: center;
      padding: 24px;
      background: rgba(19,32,42,0.48);
    }}
    .person-preview-modal[hidden] {{ display: none; }}
    .person-preview-panel {{
      width: min(1100px, 96vw);
      height: min(880px, 92vh);
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      border-radius: 12px;
      background: var(--bg);
      box-shadow: 0 24px 80px rgba(19,32,42,0.32);
      overflow: hidden;
    }}
    .person-preview-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.82);
    }}
    .person-preview-head h2 {{ margin: 0; font-size: 1.1rem; }}
    .person-preview-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 10px;
      padding: 14px;
      overflow-y: auto;
      min-height: 0;
      overscroll-behavior: contain;
    }}
    .person-preview-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: rgba(255,255,255,0.8);
      position: relative;
    }}
    .person-preview-card.selected {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(204,95,50,0.22);
    }}
    .preview-card-check {{
      position: absolute;
      top: 6px;
      left: 6px;
      z-index: 2;
      width: 20px;
      height: 20px;
      accent-color: var(--accent);
      cursor: pointer;
    }}
    .person-preview-thumb {{
      aspect-ratio: 2 / 3;
      background: rgba(19,32,42,0.08);
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: .82rem;
      cursor: pointer;
    }}
    .person-preview-thumb img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .person-preview-empty {{ padding: 24px; color: var(--muted); }}
    .preview-bulk-bar {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      padding: 10px 16px;
      border-top: 1px solid var(--line);
      background: rgba(255,248,244,0.96);
      font-size: .86rem;
    }}
    .preview-bulk-bar[hidden] {{ display: none; }}
    .preview-bulk-bar strong {{ white-space: nowrap; }}
    .preview-bulk-bar select {{
      flex: 1;
      min-width: 140px;
      min-height: 32px;
      padding: 4px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: white;
      font-size: .84rem;
    }}
    .preview-bulk-move {{ padding: 5px 14px; border: none; border-radius: 6px; background: var(--accent); color: white; cursor: pointer; font-size: .84rem; white-space: nowrap; }}
    .preview-bulk-move:disabled {{ opacity: 0.45; cursor: default; }}
    .preview-bulk-unassign {{ padding: 5px 10px; border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--text); cursor: pointer; font-size: .84rem; white-space: nowrap; }}
    .preview-bulk-unassign:disabled {{ opacity: 0.45; cursor: default; }}
    .preview-bulk-state {{ color: var(--muted); font-size: .8rem; }}
    .person-preview-tools {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) auto;
      gap: 8px;
      padding: 0 0 12px;
    }}
    .person-preview-tools small {{
      align-self: center;
      color: var(--muted);
      font-weight: 800;
      white-space: nowrap;
    }}
    .person-preview-reassign {{
      display: flex;
      align-items: center;
      gap: 5px;
      padding: 6px 8px;
      border-top: 1px solid var(--line);
      background: rgba(19,32,42,0.03);
    }}
    .person-preview-face {{
      width: 36px;
      height: 36px;
      border-radius: 50%;
      object-fit: cover;
      flex-shrink: 0;
      border: 1px solid var(--line);
    }}
    .person-preview-reassign select {{
      flex: 1;
      min-width: 0;
      font-size: .72rem;
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 3px 4px;
      background: white;
      color: var(--text);
    }}
    .reassign-btn {{
      font-size: .72rem;
      padding: 3px 7px;
      border: none;
      border-radius: 4px;
      background: var(--accent);
      color: white;
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
    }}
    .reassign-btn:disabled {{ opacity: 0.45; cursor: default; }}
    .empty-panel {{
      margin-top: 14px;
      padding: 14px;
      border: 1px dashed var(--line);
      border-radius: 10px;
      color: var(--muted);
      background: rgba(255,255,255,0.48);
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .ai-spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid var(--accent); border-top-color: transparent; border-radius: 50%; animation: spin .8s linear infinite; vertical-align: middle; margin-right: 4px; }}
    .debug-form {{
      display: grid;
      gap: 10px;
      margin-top: 16px;
    }}
    .debug-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.3fr) repeat(5, minmax(112px, .42fr)) auto;
      gap: 8px;
    }}
    .debug-grid input, .debug-grid select {{
      min-height: 40px;
      padding: 9px 12px;
      border: 1px solid rgba(19,32,42,0.14);
      border-radius: 14px;
      background: rgba(255,255,255,0.9);
      color: var(--text);
      font: .9rem "Inter", "Helvetica Neue", sans-serif;
    }}
    .debug-grid button {{
      min-height: 40px;
      padding: 9px 14px;
      border: 0;
      border-radius: 999px;
      background: #174f49;
      color: white;
      font-weight: 800;
      cursor: pointer;
    }}
    .debug-result {{
      margin: 0;
      padding: 10px 12px;
      min-height: 260px;
      overflow: auto;
      border-radius: 14px;
      background: rgba(19,32,42,0.05);
      color: var(--text);
      font: .8rem "SFMono-Regular", "Menlo", monospace;
      line-height: 1.45;
      white-space: pre-wrap;
    }}
    .benchmark-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    .benchmark-actions button {{
      min-height: 40px;
      padding: 9px 14px;
      border: 0;
      border-radius: 999px;
      background: #13202a;
      color: white;
      font-weight: 800;
      cursor: pointer;
    }}
    .benchmark-summary {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 8px;
      color: var(--muted);
      font-size: .9rem;
    }}
    code {{
      font-family: "SFMono-Regular", "Menlo", monospace;
      font-size: .84rem;
    }}
    @media (max-width: 920px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .card {{ grid-column: 1 / -1; }}
      .metric-grid {{ grid-template-columns: 1fr 1fr; }}
      .mini-grid {{ grid-template-columns: 1fr; }}
      .compact-list {{ grid-template-columns: 1fr; }}
      .debug-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .manager-disclosure summary {{ grid-template-columns: 1fr; }}
      .people-tools, .person-preview-tools {{ grid-template-columns: 1fr; }}
      .person-row {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 560px) {{
      .metric-grid {{ grid-template-columns: 1fr; }}
      .debug-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
        <div>
        <span class="eyebrow">로컬 사진 검색</span>
        <h1>사진을 가져오고, 바로 찾습니다.</h1>
        <p>원본은 로컬/NAS에 두고, 이 화면에서는 사진 가져오기와 검색 분석 상태만 확인합니다.</p>
        <div class="pill-row" style="margin-top:14px;">
          <span class="pill"><strong>실행 방식</strong> {escape(security["deployment_label"])}</span>
          <span class="pill"><strong>보안</strong> {'오프라인 전용' if not security['outbound_network_enabled'] else '온라인 준비 모드'}</span>
        </div>
        <div class="hero-links">
          <a class="link-btn" href="/gallery">사진첩 열기</a>
        </div>
      </div>
      <div class="metric-grid">
        <div class="metric clickable" onclick="openDetailPopup('files','파일 현황 기준')">
          파일 현황
          <strong id="m-total">{catalog["breakdown"]["total"]}개</strong>
          <small id="m-total-kinds">사진 {catalog["kind_counts"].get("image", 0)}개</small>
          <small id="m-total-status">{escape(catalog["breakdown"]["summary_text"])}</small>
        </div>
        <div class="metric clickable" onclick="openDetailPopup('tags','자동 태그 목록')">
          자동 태그
          <strong id="m-tags">{performance["generated_tags"]}</strong>
          <small id="m-tags-note">{performance["tagged_media"]}개 사진에 적용</small>
        </div>
        <div class="metric clickable" onclick="openDetailPopup('places','장소 정보')">
          장소 정보
          <strong id="m-places">{performance["place_tagged_media"]}</strong>
          <small>GPS/장소 태그로 찾을 수 있는 사진</small>
        </div>
        <div class="metric clickable" onclick="openDetailPopup('people','인물 목록')">
          인물 정보
          <strong id="m-people">{performance["person_count"]}</strong>
          <small id="m-people-note">{performance["people_media"]}개 사진에서 얼굴 감지</small>
        </div>
        <div class="metric clickable" onclick="openDetailPopup('ai','이미지 AI 태그')">
          <span class="metric-label-row">
            <span>이미지 AI</span>
            <span id="m-ai-state" class="{ai_metric_state_class}">{escape(ai_metric_state_label)}</span>
          </span>
          <strong id="m-ai">{performance["clip_embeddings"]} / {performance["eligible_media"]}</strong>
          <small id="m-ai-note">{escape(performance["ai_summary"]["summary_text"])}</small>
          <small id="m-ai-state-note" class="metric-status-note">{escape(ai_metric_state_detail)}</small>
        </div>
      </div>

      <div class="detail-overlay" id="detail-overlay" onclick="if(event.target===this)closeDetailPopup()">
        <div class="detail-dialog">
          <div class="detail-header">
            <h3 id="detail-title">—</h3>
            <button class="detail-close" onclick="closeDetailPopup()">✕</button>
          </div>
          <div class="detail-body" id="detail-body">
            <div class="detail-loading">불러오는 중...</div>
          </div>
        </div>
      </div>
    </section>

    <section class="grid">
      <article class="{phase1_card_class}" id="phase1-card">
        <h2 class="scan-title">라이브러리 동기화 <span id="phase1-state-badge" class="run-badge {'is-running' if phase1_active else ''}">{phase1_state_label}</span> <span id="nas-status-badge" class="nas-badge nas-unknown">NAS 확인 중…</span></h2>
        <p class="sub">원본 가져오기, 썸네일 생성, 검색 분석, 자동 태그 갱신을 한 루프에서 계속 처리합니다.</p>
        <div id="phase1-live-panel" class="{phase1_live_class}">
          <strong>현재 상태</strong><br>
          <span id="phase1-live-detail">{escape(phase1_live_text)}</span>
        </div>
        <div class="pill-row">
          <span class="pill"><strong>상태</strong> <span id="phase1-state-text" class="{phase1_state_class}">{phase1_state_label}</span></span>
          <button type="button" class="pill pill-button" id="phase1-schedule-button" title="자동 실행 간격 변경"><strong>자동 실행</strong> {phase1_schedule_label}</button>
        </div>
        <div class="list compact-list" style="margin-top:14px;">
          <div class="row"><span>진행</span><span id="p1-progress" class="live-status">—</span></div>
          <div class="row"><span>다음 실행</span><span id="p1-next-scan">{escape(str(scheduler.get('next_library_run_at')))}</span></div>
          <div class="row"><span>마지막 실행</span><span id="p1-last-scan">{escape(str(scheduler.get('last_library_run_at')))}</span></div>
          <div class="row"><span>검색 준비</span><span id="p1-search">{semantic_coverage["search_current"]} / {semantic_coverage["eligible_media"]}</span></div>
          <div class="row"><span>남은 분석</span><span id="p1-pending">{semantic_coverage["remaining_for_search"] + semantic_coverage["remaining_for_clip"]}</span></div>
          <div class="row"><span>누락 파일</span><span id="p1-missing" class="{'status-warn' if health['missing'] else ''}">{health["missing"]}</span></div>
        </div>
        <form class="scan-form" id="phase1-scan-form" onsubmit="return false">
          <label>
            사진 폴더
            <textarea id="phase1-source-roots" name="source_roots" spellcheck="false">{source_roots_text}</textarea>
            <span class="field-help">{escape(source_root_guidance)} 한 줄에 폴더 하나씩 입력합니다. 선택한 폴더 아래의 모든 하위 폴더까지 재귀적으로 포함합니다.</span>
          </label>
          <div class="scan-actions">
            <button type="button" id="source-picker-open">폴더 선택</button>
            <span class="field-help">대부분은 Finder 저장장치에서 NAS/외장하드/USB 폴더를 고르면 됩니다.</span>
          </div>
          <div class="field-help">
            경로 구분: Finder 저장장치는 macOS에 마운트된 NAS·외장하드·USB입니다. Mac 사용자 폴더는 Desktop/Pictures/Photos 같은 로컬 폴더입니다. Docker 내부 사진 폴더(/photos)는 환경변수로 따로 붙인 호환용 경로라 보통은 선택하지 않아도 됩니다.
          </div>
          <div class="source-picker" id="source-picker" hidden>
            <div class="source-picker-bar">
              <button type="button" id="source-picker-home">바로가기</button>
              <button type="button" id="source-picker-up">위로</button>
              <span class="source-picker-path" id="source-picker-path">바로가기</span>
            </div>
            <div class="source-picker-list" id="source-picker-list" aria-live="polite"></div>
            <span class="field-help" id="source-picker-note"></span>
          </div>
          <div class="scan-actions">
            <button type="button" id="phase1-scan-button"{phase1_scan_disabled}>전체 동기화 시작</button>
            <button type="button" id="phase1-retry-button"{phase1_scan_disabled}>오류 항목만 재처리</button>
          </div>
          <div class="field-help">
            전체 동기화는 선택한 폴더 전체를 다시 훑어서 새 사진·이동·누락을 반영하고, 썸네일/검색/자동태그까지 이어서 채웁니다. 오류 항목만 재처리는 지난 처리에서 실패한 사진만 다시 시도합니다.
          </div>
          <pre class="scan-result" id="phase1-scan-result" aria-live="polite"></pre>
        </form>
      </article>

      <article class="{phase2_card_class}" id="phase2-card" style="display:none">
        <h2 class="scan-title">검색 개선 <span id="phase2-state-badge" class="run-badge {'is-running' if phase2_active else ''}">{phase2_state_label}</span></h2>
        <p class="sub">사진을 분석해서 바다, 아기, 장소, 사람 이름 같은 검색어로 찾을 수 있게 만듭니다.</p>
        <div id="phase2-live-panel" class="{phase2_live_class}">
          <strong>현재 상태</strong><br>
          <span id="phase2-live-detail">{escape(phase2_live_text)}</span>
        </div>
        <div class="pill-row">
          <span class="pill"><strong>상태</strong> <span id="phase2-state-text" class="{phase2_state_class}">{phase2_state_label}</span></span>
        </div>
        <div class="list compact-list" style="margin-top:14px;">
          <div class="row"><span>진행</span><span id="p2-progress" class="live-status">—</span></div>
          <div class="row"><span>검색 준비</span><span id="p2-search">{semantic_coverage["search_current"]} / {semantic_coverage["eligible_media"]}</span></div>
          <div class="row"><span>남은 작업</span><span id="p2-pending">{semantic_coverage["remaining_for_search"] + semantic_coverage["remaining_for_clip"]}</span></div>
          <div class="row"><span>자동 태그</span><span id="p2-auto-tags">{semantic_coverage["auto_tag_states_current"]}</span></div>
          {'<div class="row"><span>얼굴 재분석</span><span id="p2-face-pending">' + str(semantic_coverage["remaining_for_face_reanalysis"]) + '</span></div>' if settings.face_analysis_enabled else ''}
          <div class="row"><span>오류</span><span id="p2-errors">{semantic_coverage["semantic_job_errors"]}</span></div>
        </div>
        <form class="scan-form" id="phase2-semantic-form" onsubmit="return false">
          <div class="scan-actions">
            <button type="button" id="phase2-semantic-button"{phase2_run_disabled}>지금 분석</button>
            <button type="button" id="phase2-cancel-button" style="{phase2_cancel_display}">중지</button>
          </div>
          <pre class="scan-result" id="phase2-semantic-result" aria-live="polite"></pre>
        </form>
      </article>

      <details class="card full manager-disclosure" id="people-manager-card">
        <summary>
          <div>
            <h2>사람 이름</h2>
            <p class="sub">이름이 필요한 얼굴부터 빠르게 정리하고, 저장한 이름과 애칭은 바로 검색에 반영됩니다.</p>
          </div>
          <span class="disclosure-pill">{escape(people_manager_summary)} · 열기</span>
        </summary>
        <div class="manager-body">
          <p class="sub">반복해서 나온 얼굴만 올리고, 이름이 비어 있으면 첫 애칭을 대표 이름으로 바로 승격합니다.</p>
          <div class="people-queue-summary">
            <span class="people-queue-pill">전체 <strong id="people-total-count">{int(people_stats.get("total") or len(people))}명</strong></span>
            <span class="people-queue-pill">이름 필요 <strong id="people-unnamed-count">{int(people_stats.get("unnamed") or 0)}명</strong></span>
            <span class="people-queue-pill">이름 있음 <strong id="people-named-count">{int(people_stats.get("named") or 0)}명</strong></span>
          </div>
          <div class="people-mode-switch" id="people-mode-switch">
            <button type="button" class="people-mode-btn active" data-mode="unnamed">이름 필요만</button>
            <button type="button" class="people-mode-btn" data-mode="named">이름 있는 사람</button>
            <button type="button" class="people-mode-btn" data-mode="all">전체</button>
          </div>
          <div class="people-tools">
            <input id="people-filter" type="search" placeholder="이름, 애칭, person id로 찾기">
            <select id="people-sort" aria-label="Sort people">
              <option value="photos">사진 많은 순</option>
              <option value="faces">얼굴 많은 순</option>
              <option value="named">이름 있음 먼저</option>
              <option value="unnamed">이름 없음 먼저</option>
              <option value="name">이름순</option>
            </select>
            <small id="people-visible-count">0명</small>
          </div>
          <div class="people-merge-bar" id="people-merge-bar" hidden>
            <span id="people-merge-count">0개 선택</span>
            <label>남길 사람 <select id="people-merge-target"></select></label>
            <button type="button" class="btn-sm" id="people-merge-button">선택 병합</button>
            <small id="people-merge-state" aria-live="polite"></small>
          </div>
          <div class="person-list" id="people-manager">
            {people_manager_html}
          </div>
          <div class="people-more-row">
            <button type="button" class="btn-copy" id="people-load-more" hidden>더 보기</button>
          </div>
        </div>
      </details>

      <article class="card full admin-only">
        <h2>Processing Versions</h2>
        <p class="sub">Current version stamp for each AI analysis step. When a version changes, affected photos are automatically re-processed.</p>
        <div class="pill-row">
          <span class="pill"><strong>Place</strong> <code>{escape(semantic['versions']['place'])}</code></span>
          <span class="pill"><strong>Person</strong> <code>{escape(semantic['versions']['person'])}</code></span>
          <span class="pill"><strong>OCR</strong> <code>{escape(semantic['versions']['ocr'])}</code></span>
          <span class="pill"><strong>Caption</strong> <code>{escape(semantic['versions']['caption'])}</code></span>
          <span class="pill"><strong>Embedding</strong> <code>{escape(semantic['versions']['embedding'])}</code></span>
          <span class="pill"><strong>Auto Tags</strong> <code>{escape(semantic['versions']['auto_tags'])}</code></span>
          <span class="pill"><strong>Search</strong> <code>{escape(semantic['versions']['search'])}</code></span>
        </div>
      </article>

      <!-- 이미지 AI 설치/활성화 카드는 제거됨: 정식 배포 DMG가 CLIP 패키지와
           weights를 항상 번들하므로 설치/다운로드 안내가 불필요하다. AI 진행
           상태는 상단 "이미지 AI" 지표 타일에서 확인한다. -->

      <article class="card full" id="resource-settings-card">
        <h2>리소스 설정</h2>
        <p class="sub">이 Mac의 CPU/메모리 자원을 포토미가 얼마나 세게 쓸지 정합니다. CPU는 동시 처리 수와 AI 스레드, 메모리는 한 번에 잡아먹는 분석 묶음 크기로 조절합니다.</p>
        <div class="pill-row">
          <span class="pill"><strong>CPU 강도</strong> <span id="resource-cpu-profile">{escape(str(resource_settings.get("cpu_profile_label") or "균형"))}</span></span>
          <span class="pill"><strong>메모리 압력</strong> <span id="resource-memory-profile">{escape(str(resource_settings.get("memory_profile_label") or "보통"))}</span></span>
          <span class="pill"><strong>설정 파일</strong> <code>{escape(str(resource_settings.get("env_file") or ".env"))}</code></span>
        </div>
        <form class="scan-form" id="resource-settings-form" onsubmit="return false">
          <label>
            CPU 병렬 처리
            <input type="range" id="resource-workers" min="1" max="{int(resource_settings.get('asset_processing_workers_cap') or 1)}" value="{int(resource_settings.get('asset_processing_workers') or 1)}">
            <span class="field-help">동시 처리 수 <strong id="resource-workers-value">{int(resource_settings.get('asset_processing_workers') or 1)}</strong> / {int(resource_settings.get('asset_processing_workers_cap') or 1)}. 높일수록 전체 동기화 때 CPU를 더 세게 씁니다.</span>
          </label>
          <label>
            AI CPU 스레드
            <input type="range" id="resource-torch-threads" min="1" max="{int(resource_settings.get('torch_threads_cap') or 1)}" value="{int(resource_settings.get('torch_threads') or 1)}">
            <span class="field-help">CLIP/OCR 쪽 스레드 <strong id="resource-torch-threads-value">{int(resource_settings.get('torch_threads') or 1)}</strong> / {int(resource_settings.get('torch_threads_cap') or 1)}. 높일수록 이미지 AI가 CPU를 더 적극적으로 씁니다.</span>
          </label>
          <label>
            백그라운드 AI 묶음 크기
            <input type="number" id="resource-maintenance-batch" min="50" max="5000" step="50" value="{int(resource_settings.get('semantic_maintenance_batch_size') or 500)}">
            <span class="field-help">자동으로 뒤에서 도는 이미지 AI 묶음입니다. 클수록 빠르지만 메모리를 더 씁니다.</span>
          </label>
          <label>
            수동 AI 묶음 크기
            <input type="number" id="resource-manual-batch" min="50" max="5000" step="50" value="{int(resource_settings.get('semantic_manual_batch_size') or 1000)}">
            <span class="field-help">"지금 분석" 눌렀을 때 한 번에 처리할 양입니다.</span>
          </label>
          <div class="scan-actions">
            <button type="button" id="resource-settings-save">저장</button>
            <span class="field-help">저장하면 다음 동기화/이미지 AI 작업부터 반영됩니다. 이미 돌고 있는 작업은 끝날 때까지 기존 값으로 갑니다.</span>
          </div>
          <pre class="scan-result" id="resource-settings-result" aria-live="polite"></pre>
        </form>
      </article>

      <article class="card full admin-only">
        <h2>System Tools</h2>
        <p class="sub">Status of local tools and AI models required by photome.</p>
        <div class="pill-row">
          <span class="pill"><strong>Mode</strong> {escape(security["runtime_mode"])}</span>
          <span class="pill"><strong>Deployment</strong> {escape(security["deployment_label"])}</span>
          <span class="pill"><strong>Network</strong> {'offline · blocked' if not security['outbound_network_enabled'] else 'online'}</span>
        </div>
        <div class="list" style="margin-top:14px;">
          {''.join(f'<div class="row"><span>{escape(item["name"])}</span><span>{escape(item["state"])}</span></div>' for item in security["local_dependencies"])}
          {''.join(f'<div class="row"><span>Blocked</span><span>{escape(item)}</span></div>' for item in security["disabled_features"])}
        </div>
      </article>

      <article class="card full admin-only">
        <h2>Source and Storage</h2>
        <p class="sub">Source roots are NAS/original media paths as seen by the current deployment. Derived root and database are local generated/cache storage and should not be used as source roots.</p>
        <div class="list">
          <div class="row"><span>Path rule</span><span>{escape(source_root_guidance)}</span></div>
          <div class="row"><span>Configured source roots</span><span>{'<br>'.join(escape(path) for path in source_roots)}</span></div>
          <div class="row"><span>Cataloged source roots</span><span>{known_source_roots_html}</span></div>
          <div class="row"><span>Local derived/cache root</span><span><code>{escape(payload['storage']['derived_root'])}</code></span></div>
          <div class="row"><span>Local database</span><span><code>{escape(payload['storage']['database_url'])}</code></span></div>
          <div class="row"><span>Recent jobs tracked</span><span>{len(jobs['recent'])}</span></div>
        </div>
      </article>

      <details class="card full manager-disclosure admin-only" id="search-inspector-card">
        <summary>
          <div>
            <h2>Search Inspector</h2>
            <p class="sub">Developer tool for checking search routing, planner terms, channel weights, and benchmark cases.</p>
          </div>
          <span class="disclosure-pill">debug · click to open</span>
        </summary>
        <div class="manager-body">
        <form class="debug-form" id="search-debug-form">
          <div class="debug-grid">
            <input id="search-debug-query" name="q" placeholder="Search query" value="작년 여름 바다에서 가족이랑 찍은 사진">
            <select id="search-debug-mode" name="mode">
              <option value="hybrid" selected>Auto (hybrid)</option>
              <option value="ocr">Text / OCR</option>
              <option value="semantic">Image AI</option>
            </select>
            <input id="search-debug-place" name="place" placeholder="Place filter (optional)">
            <input id="search-debug-w-ocr" name="w_ocr" placeholder="OCR weight" inputmode="decimal" title="Weight for text/OCR channel (0–1, blank = auto)">
            <input id="search-debug-w-clip" name="w_clip" placeholder="AI weight" inputmode="decimal" title="Weight for AI image channel (0–1, blank = auto)">
            <input id="search-debug-w-shadow" name="w_shadow" placeholder="Keyword weight" inputmode="decimal" title="Weight for keyword/tag channel (0–1, blank = auto)">
            <button type="submit">Inspect</button>
          </div>
          <pre class="debug-result" id="search-debug-result" aria-live="polite"></pre>
        </form>
        <div class="benchmark-actions">
          <button type="button" id="search-benchmark-run">Run Search Quality Check</button>
          <div class="benchmark-summary" id="search-benchmark-summary"></div>
        </div>
        <pre class="debug-result" id="search-benchmark-result" aria-live="polite"></pre>
        </div>
      </details>
    </section>
  </main>
  <div class="person-preview-modal" id="person-preview-modal" hidden>
    <section class="person-preview-panel" role="dialog" aria-modal="true" aria-labelledby="person-preview-title">
      <header class="person-preview-head">
        <div>
          <h2 id="person-preview-title">사진 확인</h2>
          <p class="sub" id="person-preview-subtitle">불러오는 중</p>
        </div>
        <button type="button" class="btn-copy" id="person-preview-close">닫기</button>
      </header>
      <div class="person-preview-tools">
        <input id="person-reassign-filter" type="search" placeholder="옮길 이름 또는 애칭 찾기">
        <small id="person-reassign-count">0명</small>
      </div>
      <div class="person-preview-grid" id="person-preview-grid"></div>
      <div class="preview-bulk-bar" id="preview-bulk-bar" hidden>
        <strong id="preview-bulk-count">0장 선택</strong>
        <select id="preview-bulk-target" title="이동할 인물 선택">
          <option value="">— 인물 선택 —</option>
        </select>
        <button type="button" class="preview-bulk-move" id="preview-bulk-move" disabled>이동</button>
        <button type="button" class="preview-bulk-unassign" id="preview-bulk-unassign">할당 해제</button>
        <span class="preview-bulk-state" id="preview-bulk-state"></span>
        <button type="button" class="btn-copy" id="preview-bulk-cancel" style="margin-left:auto">선택 해제</button>
      </div>
    </section>
  </div>
  <script>
    const scanForm = document.getElementById("phase1-scan-form");
    const scanResult = document.getElementById("phase1-scan-result");
    const scanCard = document.getElementById("phase1-card");
    const scanButton = document.getElementById("phase1-scan-button");
    const phase1RetryButton = document.getElementById("phase1-retry-button");
    const phase1ScheduleButton = document.getElementById("phase1-schedule-button");
    const sourceRootsField = document.getElementById("phase1-source-roots");
    const sourcePickerOpen = document.getElementById("source-picker-open");
    const sourcePicker = document.getElementById("source-picker");
    const sourcePickerHome = document.getElementById("source-picker-home");
    const sourcePickerUp = document.getElementById("source-picker-up");
    const sourcePickerPath = document.getElementById("source-picker-path");
    const sourcePickerList = document.getElementById("source-picker-list");
    const sourcePickerNote = document.getElementById("source-picker-note");
    const semanticForm = document.getElementById("phase2-semantic-form");
    const semanticResult = document.getElementById("phase2-semantic-result");
    const semanticCard = document.getElementById("phase2-card");
    const semanticButton = document.getElementById("phase2-semantic-button");
    const semanticCancelButton = document.getElementById("phase2-cancel-button");
    const resourceForm = document.getElementById("resource-settings-form");
    const resourceSaveButton = document.getElementById("resource-settings-save");
    const resourceResult = document.getElementById("resource-settings-result");
    const resourceWorkers = document.getElementById("resource-workers");
    const resourceWorkersValue = document.getElementById("resource-workers-value");
    const resourceTorchThreads = document.getElementById("resource-torch-threads");
    const resourceTorchThreadsValue = document.getElementById("resource-torch-threads-value");
    const resourceMaintenanceBatch = document.getElementById("resource-maintenance-batch");
    const resourceManualBatch = document.getElementById("resource-manual-batch");
    const resourceCpuProfile = document.getElementById("resource-cpu-profile");
    const resourceMemoryProfile = document.getElementById("resource-memory-profile");
    const phase1StorageKey = "photome.dashboard.phase1.job";
    const phase2StorageKey = "photome.dashboard.phase2.job";
    const phase1SourceRootsStorageKey = "photome.dashboard.phase1.source_roots";
    const peopleManagerOpenStorageKey = "photome.dashboard.people_manager.open";
    let isPeopleMergeInProgress = false;
    let isPeopleSaveInProgress = false;
    const faceSamplePlaceholder = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
    let activeLibraryJob = {active_library_job_json};
    let schedulerSnapshot = {json.dumps(scheduler, default=str)};
    let performanceSnapshot = {json.dumps(performance, default=str)};
    let semanticCoverageSnapshot = {json.dumps(semantic_coverage, default=str)};
    const allPeopleForReassign = {people_json};
    let currentPreviewPersonId = null;
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const personPreviewModal = document.getElementById("person-preview-modal");
    const personPreviewTitle = document.getElementById("person-preview-title");
    const personPreviewSubtitle = document.getElementById("person-preview-subtitle");
    const personPreviewGrid = document.getElementById("person-preview-grid");
    const personPreviewClose = document.getElementById("person-preview-close");
    const personPreviewCache = new Map();
    const previewBulkBar = document.getElementById("preview-bulk-bar");
    const previewBulkCount = document.getElementById("preview-bulk-count");
    const previewBulkTarget = document.getElementById("preview-bulk-target");
    const previewBulkMove = document.getElementById("preview-bulk-move");
    const previewBulkUnassign = document.getElementById("preview-bulk-unassign");
    const previewBulkState = document.getElementById("preview-bulk-state");
    const previewBulkCancel = document.getElementById("preview-bulk-cancel");
    const aiOnlineCmd = {json.dumps(online_ai_cmd)};
    const aiOfflineCmd = {json.dumps(offline_ai_cmd)};
    const aiActivateCmd = {json.dumps(activate_ai_cmd)};
    const personReassignFilter = document.getElementById("person-reassign-filter");
    const personReassignCount = document.getElementById("person-reassign-count");
    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, (ch) => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[ch]));
    }}
    function updateResourceLabels() {{
      const workers = Number(resourceWorkers?.value || 1);
      const workerCap = Number(resourceWorkers?.max || workers || 1);
      const torchThreads = Number(resourceTorchThreads?.value || 1);
      const maintenanceBatch = Number(resourceMaintenanceBatch?.value || 500);
      const manualBatch = Number(resourceManualBatch?.value || 1000);
      if (resourceWorkersValue) resourceWorkersValue.textContent = String(workers);
      if (resourceTorchThreadsValue) resourceTorchThreadsValue.textContent = String(torchThreads);
      if (resourceCpuProfile) {{
        const ratio = workers / Math.max(1, workerCap);
        resourceCpuProfile.textContent = ratio >= 0.85 ? "최대" : ratio >= 0.6 ? "고성능" : ratio >= 0.35 ? "균형" : "절약";
      }}
      if (resourceMemoryProfile) {{
        const score = Math.max(maintenanceBatch, manualBatch);
        resourceMemoryProfile.textContent = score >= 1500 ? "높음" : score >= 700 ? "보통" : "낮음";
      }}
    }}
    async function saveResourceSettings() {{
      if (!resourceSaveButton) return;
      resourceSaveButton.disabled = true;
      if (resourceResult) resourceResult.textContent = "저장 중...";
      try {{
        const response = await fetch("/settings/performance", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{
            asset_processing_workers: Number(resourceWorkers?.value || 1),
            torch_threads: Number(resourceTorchThreads?.value || 1),
            semantic_maintenance_batch_size: Number(resourceMaintenanceBatch?.value || 500),
            semantic_manual_batch_size: Number(resourceManualBatch?.value || 1000),
          }}),
        }});
        const data = await response.json().catch(() => ({{message: "설정 저장에 실패했습니다."}}));
        if (!response.ok) throw new Error(data?.detail || data?.message || "설정 저장 실패");
        if (data?.settings) {{
          if (resourceWorkers && data.settings.asset_processing_workers) resourceWorkers.value = String(data.settings.asset_processing_workers);
          if (resourceTorchThreads && data.settings.torch_threads) resourceTorchThreads.value = String(data.settings.torch_threads);
          if (resourceMaintenanceBatch && data.settings.semantic_maintenance_batch_size) resourceMaintenanceBatch.value = String(data.settings.semantic_maintenance_batch_size);
          if (resourceManualBatch && data.settings.semantic_manual_batch_size) resourceManualBatch.value = String(data.settings.semantic_manual_batch_size);
          performanceSnapshot.resource_settings = data.settings;
        }}
        updateResourceLabels();
        if (resourceResult) resourceResult.textContent = data?.message || "저장 완료";
      }} catch (error) {{
        if (resourceResult) resourceResult.textContent = error?.message || "설정 저장 실패";
      }} finally {{
        resourceSaveButton.disabled = false;
      }}
    }}
    [resourceWorkers, resourceTorchThreads, resourceMaintenanceBatch, resourceManualBatch].forEach((element) => {{
      element?.addEventListener("input", updateResourceLabels);
    }});
    resourceSaveButton?.addEventListener("click", saveResourceSettings);
    updateResourceLabels();
    function personSearchText(person) {{
      return [
        person.display_name,
        ...(person.aliases || []),
        `person-${{String(person.id).padStart(6, "0")}}`,
      ].join(" ").toLowerCase();
    }}
    function personOptionLabel(person) {{
      const aliases = (person.aliases || []).slice(0, 2).join(", ");
      const suffix = aliases ? ` · ${{aliases}}` : "";
      return `${{person.display_name}}${{suffix}} · ${{person.media_count || 0}} photos`;
    }}
    function reassignTargets() {{
      const filter = (personReassignFilter?.value || "").trim().toLowerCase();
      return allPeopleForReassign
        .filter((person) => person.id !== currentPreviewPersonId)
        .filter((person) => !filter || personSearchText(person).includes(filter))
        .slice(0, 40);
    }}
    function reassignOptionsHtml(selectedValue = "") {{
      const options = reassignTargets()
        .map((person) => `<option value="${{person.id}}" ${{String(person.id) === String(selectedValue) ? "selected" : ""}}>${{escapeHtml(personOptionLabel(person))}}</option>`)
        .join("");
      return `<option value="">— 이동 —</option>${{options}}<option value="unassign" ${{selectedValue === "unassign" ? "selected" : ""}}>배정 해제</option>`;
    }}
    function refreshReassignSelects() {{
      const targets = reassignTargets();
      if (personReassignCount) personReassignCount.textContent = `${{targets.length}}명`;
      personPreviewGrid?.querySelectorAll(".reassign-select").forEach((select) => {{
        const previous = select.value;
        select.innerHTML = reassignOptionsHtml(previous);
        if (previous && Array.from(select.options).some((option) => option.value === previous)) {{
          select.value = previous;
        }}
        const btn = personPreviewGrid.querySelector(`.reassign-btn[data-face-id="${{select.dataset.faceId}}"]`);
        if (btn) btn.disabled = !select.value;
      }});
    }}
    function closePersonPreview() {{
      if (personPreviewModal) personPreviewModal.hidden = true;
      if (personPreviewGrid) personPreviewGrid.innerHTML = "";
      if (previewBulkBar) previewBulkBar.hidden = true;
      document.body.style.overflow = "";
    }}
    function renderPersonPreview(payload, fallbackLabel, fallbackId) {{
      const items = payload?.items || [];
      if (personPreviewTitle) personPreviewTitle.textContent = payload?.person?.display_name || fallbackLabel || `person-${{fallbackId}}`;
      if (personPreviewSubtitle) personPreviewSubtitle.textContent = `${{items.length}}개 사진`;
      personPreviewGrid.innerHTML = items.length
        ? items.map(personPreviewCard).join("")
        : '<div class="person-preview-empty">No active photos for this person.</div>';
      refreshReassignSelects();
    }}
    function selectedPreviewCards() {{
      return Array.from(personPreviewGrid?.querySelectorAll(".person-preview-card") || [])
        .filter((card) => card.querySelector(".preview-card-check")?.checked);
    }}
    function updateBulkBar() {{
      const selected = selectedPreviewCards();
      if (!previewBulkBar) return;
      previewBulkBar.hidden = selected.length === 0;
      if (previewBulkCount) previewBulkCount.textContent = `${{selected.length}}장 선택`;
      if (previewBulkMove) previewBulkMove.disabled = !previewBulkTarget?.value;
      if (previewBulkState) previewBulkState.textContent = "";
    }}
    function populateBulkTarget() {{
      if (!previewBulkTarget) return;
      const options = reassignTargets()
        .map((p) => `<option value="${{p.id}}">${{escapeHtml(personOptionLabel(p))}}</option>`)
        .join("");
      previewBulkTarget.innerHTML = `<option value="">— 인물 선택 —</option>${{options}}`;
    }}
    async function bulkReassignFaces(faceIds, personIdOrNull) {{
      const results = await Promise.allSettled(
        faceIds.map((faceId) =>
          fetch(`/people/faces/${{faceId}}`, {{
            method: "PATCH",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{person_id: personIdOrNull}}),
          }})
        )
      );
      return results.filter((r) => r.status === "fulfilled" && r.value.ok).length;
    }}
    function personPreviewCard(item) {{
      const thumb = item.asset_id
        ? `<img src="/gallery/assets/${{item.asset_id}}" alt="${{escapeHtml(item.filename)}}" loading="lazy" decoding="async" title="${{escapeHtml(item.filename)}}">`
        : `<span>${{escapeHtml(item.media_kind || "media")}}</span>`;
      const faceAttr = item.face_id ? ` data-face-id="${{item.face_id}}"` : "";
      const faceCrop = item.face_id
        ? `<img class="person-preview-face" src="/people/faces/${{item.face_id}}/crop" alt="face crop" loading="lazy" decoding="async">`
        : `<span class="person-preview-face" aria-hidden="true"></span>`;
      const reassignControls = item.face_id ? `
          <div class="person-preview-reassign">
            ${{faceCrop}}
            <select class="reassign-select" data-face-id="${{item.face_id}}" aria-label="인물 이동 대상">${{reassignOptionsHtml()}}</select>
            <button type="button" class="reassign-btn" data-face-id="${{item.face_id}}" disabled>개별 이동</button>
          </div>` : "";
      return `
        <article class="person-preview-card"${{faceAttr}} data-file-id="${{escapeHtml(item.file_id)}}">
          <input type="checkbox" class="preview-card-check" aria-label="선택">
          <div class="person-preview-thumb">${{thumb}}</div>
          ${{reassignControls}}
        </article>
      `;
    }}
    async function reassignFace(faceId, newPersonId) {{
      const body = newPersonId === "unassign" ? {{person_id: null}} : {{person_id: Number(newPersonId)}};
      const response = await fetch(`/people/faces/${{faceId}}`, {{
        method: "PATCH",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(body),
      }});
      return response.ok;
    }}
    personPreviewGrid?.addEventListener("change", (e) => {{
      if (e.target.classList.contains("preview-card-check")) {{ updateBulkBar(); return; }}
      if (e.target.classList.contains("reassign-select")) {{
        const btn = personPreviewGrid.querySelector(`.reassign-btn[data-face-id="${{e.target.dataset.faceId}}"]`);
        if (btn) btn.disabled = !e.target.value;
      }}
    }});
    personPreviewGrid?.addEventListener("click", async (e) => {{
      const reassignButton = e.target.closest(".reassign-btn");
      if (reassignButton) {{
        const faceId = Number(reassignButton.dataset.faceId || 0);
        const select = personPreviewGrid.querySelector(`.reassign-select[data-face-id="${{faceId}}"]`);
        if (!faceId || !select?.value) return;
        reassignButton.disabled = true;
        reassignButton.textContent = "처리 중";
        const ok = await reassignFace(faceId, select.value);
        if (ok) {{
          const card = reassignButton.closest(".person-preview-card");
          card?.remove();
          const remaining = personPreviewGrid?.querySelectorAll(".person-preview-card").length || 0;
          if (personPreviewSubtitle) personPreviewSubtitle.textContent = `${{remaining}}개 사진`;
          updateBulkBar();
        }} else {{
          reassignButton.textContent = "실패";
          window.setTimeout(() => {{ reassignButton.textContent = "개별 이동"; reassignButton.disabled = !select.value; }}, 900);
        }}
        return;
      }}
      // Clicking the thumb toggles selection
      const thumb = e.target.closest(".person-preview-thumb");
      if (thumb) {{
        const card = thumb.closest(".person-preview-card");
        const check = card?.querySelector(".preview-card-check");
        if (check) {{ check.checked = !check.checked; card.classList.toggle("selected", check.checked); updateBulkBar(); }}
      }}
    }});
    personReassignFilter?.addEventListener("input", () => {{ refreshReassignSelects(); populateBulkTarget(); }});
    previewBulkTarget?.addEventListener("change", () => {{
      if (previewBulkMove) previewBulkMove.disabled = !previewBulkTarget.value;
    }});
    previewBulkCancel?.addEventListener("click", () => {{
      personPreviewGrid?.querySelectorAll(".preview-card-check").forEach((cb) => {{ cb.checked = false; cb.closest(".person-preview-card")?.classList.remove("selected"); }});
      updateBulkBar();
    }});
    async function runBulkAction(personIdOrNull) {{
      const cards = selectedPreviewCards();
      const faceIds = cards.map((c) => c.dataset.faceId).filter(Boolean);
      if (!faceIds.length) return;
      [previewBulkMove, previewBulkUnassign, previewBulkCancel].forEach((b) => {{ if (b) b.disabled = true; }});
      if (previewBulkState) previewBulkState.textContent = "처리 중...";
      const ok = await bulkReassignFaces(faceIds.map(Number), personIdOrNull);
      if (previewBulkState) previewBulkState.textContent = `${{ok}}건 완료`;
      cards.forEach((card) => {{ card.style.opacity = "0.35"; card.style.pointerEvents = "none"; card.querySelector(".preview-card-check").checked = false; card.classList.remove("selected"); }});
      if (personPreviewSubtitle) {{
        const remaining = (personPreviewGrid?.querySelectorAll(".person-preview-card:not([style*='opacity'])").length || 0);
        personPreviewSubtitle.textContent = `${{remaining}}개 사진`;
      }}
      updateBulkBar();
      [previewBulkMove, previewBulkUnassign, previewBulkCancel].forEach((b) => {{ if (b) b.disabled = false; }});
      if (previewBulkMove) previewBulkMove.disabled = !previewBulkTarget?.value;
    }}
    previewBulkMove?.addEventListener("click", async () => {{
      const targetId = Number(previewBulkTarget?.value || 0);
      if (!targetId) return;
      await runBulkAction(targetId);
    }});
    previewBulkUnassign?.addEventListener("click", async () => {{
      await runBulkAction(null);
    }});
    async function openPersonPreview(personId, label) {{
      currentPreviewPersonId = Number(personId);
      if (!personPreviewModal || !personPreviewGrid) return;
      personPreviewModal.hidden = false;
      document.body.style.overflow = "hidden";
      if (previewBulkBar) previewBulkBar.hidden = true;
      if (personReassignFilter) personReassignFilter.value = "";
      refreshReassignSelects();
      populateBulkTarget();
      if (personPreviewTitle) personPreviewTitle.textContent = label || `person-${{personId}}`;
      if (personPreviewSubtitle) personPreviewSubtitle.textContent = "불러오는 중";
      personPreviewGrid.innerHTML = '<div class="person-preview-empty">Loading</div>';
      const cached = personPreviewCache.get(String(personId));
      if (cached) {{
        renderPersonPreview(cached, label, personId);
        return;
      }}
      try {{
        const response = await fetch(`/people/${{personId}}/preview?limit=30`);
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        personPreviewCache.set(String(personId), payload);
        renderPersonPreview(payload, label, personId);
      }} catch (error) {{
        if (personPreviewSubtitle) personPreviewSubtitle.textContent = "오류";
        personPreviewGrid.innerHTML = '<div class="person-preview-empty">Failed to load preview.</div>';
      }}
    }}
    personPreviewClose?.addEventListener("click", closePersonPreview);
    personPreviewModal?.addEventListener("click", (event) => {{
      if (event.target === personPreviewModal) closePersonPreview();
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.key === "Escape" && personPreviewModal && !personPreviewModal.hidden) closePersonPreview();
    }});
    document.querySelectorAll(".person-preview-trigger").forEach((button) => {{
      button.addEventListener("click", (event) => {{
        event.preventDefault();
        event.stopPropagation();
        openPersonPreview(button.dataset.personId, button.dataset.personLabel);
      }});
    }});
    const peopleMergeBar = document.getElementById("people-merge-bar");
    const peopleMergeCount = document.getElementById("people-merge-count");
    const peopleMergeTarget = document.getElementById("people-merge-target");
    const peopleMergeButton = document.getElementById("people-merge-button");
    const peopleMergeState = document.getElementById("people-merge-state");
    const peopleManagerCard = document.getElementById("people-manager-card");
    const peopleFilter = document.getElementById("people-filter");
    const peopleSort = document.getElementById("people-sort");
    const peopleVisibleCount = document.getElementById("people-visible-count");
    const peopleTotalCount = document.getElementById("people-total-count");
    const peopleUnnamedCount = document.getElementById("people-unnamed-count");
    const peopleNamedCount = document.getElementById("people-named-count");
    const peopleModeSwitch = document.getElementById("people-mode-switch");
    const peopleLoadMore = document.getElementById("people-load-more");
    const peopleStats = {json.dumps(people_stats)};
    let peopleMode = "unnamed";
    let peopleVisibleLimit = 24;
    if (peopleManagerCard) {{
      peopleManagerCard.open = localStorage.getItem(peopleManagerOpenStorageKey) === "true";
      peopleManagerCard.addEventListener("toggle", () => {{
        localStorage.setItem(peopleManagerOpenStorageKey, peopleManagerCard.open ? "true" : "false");
        if (peopleManagerCard.open) queueFaceSampleLoading();
      }});
    }}
    function personRows() {{
      return Array.from(document.querySelectorAll(".person-row"));
    }}
    let faceSampleLoadTimer = null;
    let faceSampleActiveLoads = 0;
    function visiblePersonRows() {{
      return personRows().filter((row) => !row.hidden && row.style.display !== "none");
    }}
    function queueFaceSampleLoading() {{
      if (isPeopleSaveInProgress) return;
      if (!peopleManagerCard?.open) return;
      if (faceSampleLoadTimer) return;
      faceSampleLoadTimer = window.setTimeout(() => {{
        faceSampleLoadTimer = null;
        loadVisibleFaceSamples();
      }}, 120);
    }}
    function pauseFaceSampleLoading() {{
      if (faceSampleLoadTimer) {{
        window.clearTimeout(faceSampleLoadTimer);
        faceSampleLoadTimer = null;
      }}
      document.querySelectorAll(".face-sample-img[data-loading='1']").forEach((image) => {{
        image.removeAttribute("data-loading");
        if (image.dataset.src) image.src = faceSamplePlaceholder;
      }});
      faceSampleActiveLoads = 0;
    }}
    function loadVisibleFaceSamples() {{
      if (isPeopleSaveInProgress) return;
      if (!peopleManagerCard?.open) return;
      const pending = visiblePersonRows()
        .flatMap((row) => Array.from(row.querySelectorAll(".face-sample-img[data-src]:not([data-loading='1'])")));
      const availableSlots = Math.max(0, 2 - faceSampleActiveLoads);
      pending.slice(0, availableSlots).forEach((image) => {{
        const source = image.dataset.src;
        if (!source) return;
        image.dataset.loading = "1";
        faceSampleActiveLoads += 1;
        const done = () => {{
          faceSampleActiveLoads = Math.max(0, faceSampleActiveLoads - 1);
          image.removeAttribute("data-loading");
          if (image.src.endsWith(source) || image.currentSrc.endsWith(source)) image.removeAttribute("data-src");
          queueFaceSampleLoading();
        }};
        image.addEventListener("load", done, {{ once: true }});
        image.addEventListener("error", done, {{ once: true }});
        image.src = source;
      }});
    }}
    function updatePeopleModeButtons() {{
      peopleModeSwitch?.querySelectorAll(".people-mode-btn").forEach((button) => {{
        button.classList.toggle("active", button.dataset.mode === peopleMode);
      }});
    }}
    function refreshPeopleCounts() {{
      const rows = personRows();
      const named = rows.filter((row) => Number(row.dataset.named || 0) === 1).length;
      const unnamed = rows.filter((row) => Number(row.dataset.named || 0) === 0).length;
      const total = rows.length;
      peopleStats.total = total;
      peopleStats.named = named;
      peopleStats.unnamed = unnamed;
      if (peopleTotalCount) peopleTotalCount.textContent = `${{total}}명`;
      if (peopleNamedCount) peopleNamedCount.textContent = `${{named}}명`;
      if (peopleUnnamedCount) peopleUnnamedCount.textContent = `${{unnamed}}명`;
    }}
    function applyPeopleFilterAndSort() {{
      const query = (peopleFilter?.value || "").trim().toLowerCase();
      const sortMode = peopleSort?.value || "photos";
      const rows = personRows();
      rows.forEach((row) => {{
        const matchesQuery = !query || (row.dataset.personSearch || "").includes(query);
        const isNamed = Number(row.dataset.named || 0) === 1;
        const matchesMode = peopleMode === "all" || (peopleMode === "named" ? isNamed : !isNamed);
        row.hidden = !(matchesQuery && matchesMode);
      }});
      rows.sort((a, b) => {{
        if (sortMode === "faces") return Number(b.dataset.faceCount || 0) - Number(a.dataset.faceCount || 0);
        if (sortMode === "named") return Number(b.dataset.named || 0) - Number(a.dataset.named || 0);
        if (sortMode === "unnamed") return Number(a.dataset.named || 0) - Number(b.dataset.named || 0);
        if (sortMode === "name") return (a.dataset.personLabel || "").localeCompare(b.dataset.personLabel || "");
        return Number(b.dataset.mediaCount || 0) - Number(a.dataset.mediaCount || 0);
      }});
      const list = document.getElementById("people-manager");
      rows.forEach((row) => list?.appendChild(row));
      const matched = rows.filter((row) => !row.hidden);
      matched.forEach((row, index) => {{
        const withinLimit = index < peopleVisibleLimit;
        row.style.display = withinLimit ? "" : "none";
      }});
      const visible = matched.slice(0, peopleVisibleLimit).length;
      if (peopleVisibleCount) {{
        const total = Number(peopleStats?.total || rows.length);
        peopleVisibleCount.textContent = `${{visible}} / ${{matched.length}}명 표시 · 전체 ${{total}}명`;
      }}
      if (peopleLoadMore) {{
        peopleLoadMore.hidden = matched.length <= peopleVisibleLimit;
        peopleLoadMore.textContent = `더 보기 (${{Math.max(0, matched.length - peopleVisibleLimit)}}명 남음)`;
      }}
      updatePeopleModeButtons();
      updatePeopleMergeBar();
      queueFaceSampleLoading();
    }}
    function selectedPersonRows() {{
      return personRows().filter((row) => !row.hidden && row.style.display !== "none" && row.querySelector(".person-merge-checkbox")?.checked);
    }}
    function setPeopleMergeBusy(isBusy) {{
      isPeopleMergeInProgress = isBusy;
      peopleMergeBar?.classList.toggle("is-merging", isBusy);
      if (peopleMergeTarget) peopleMergeTarget.disabled = isBusy;
      if (peopleMergeButton) {{
        peopleMergeButton.disabled = isBusy;
        peopleMergeButton.textContent = isBusy ? "병합 중" : "선택 병합";
      }}
      document.querySelectorAll(".person-merge-checkbox, .person-row button, .person-row input").forEach((element) => {{
        element.disabled = isBusy;
      }});
    }}
    function peopleCanonicalLabel(personId) {{
      return `person-${{String(personId || "").padStart(6, "0")}}`;
    }}
    function peopleMergeOptionLabel(row) {{
      const personId = row.dataset.personId || "";
      const canonical = peopleCanonicalLabel(personId);
      const label = (row.dataset.personLabel || canonical).trim();
      return label === canonical ? canonical : `${{label}} · ${{canonical}}`;
    }}
    function updatePeopleMergeBar() {{
      if (isPeopleMergeInProgress) return;
      const rows = selectedPersonRows();
      if (!peopleMergeBar || !peopleMergeTarget) return;
      peopleMergeBar.hidden = rows.length < 2;
      if (peopleMergeCount) peopleMergeCount.textContent = `${{rows.length}}개 선택`;
      const previous = peopleMergeTarget.value;
      peopleMergeTarget.replaceChildren(...rows.map((row) => {{
        const personId = row.dataset.personId || "";
        const option = document.createElement("option");
        option.value = personId;
        option.textContent = peopleMergeOptionLabel(row);
        return option;
      }}));
      if (rows.some((row) => row.dataset.personId === previous)) peopleMergeTarget.value = previous;
      if (peopleMergeState && rows.length < 2) peopleMergeState.textContent = "";
    }}
    document.querySelectorAll(".person-merge-checkbox").forEach((checkbox) => {{
      checkbox.addEventListener("change", updatePeopleMergeBar);
    }});
    peopleModeSwitch?.querySelectorAll(".people-mode-btn").forEach((button) => {{
      button.addEventListener("click", () => {{
        peopleMode = button.dataset.mode || "all";
        peopleVisibleLimit = 24;
        applyPeopleFilterAndSort();
      }});
    }});
    peopleLoadMore?.addEventListener("click", () => {{
      peopleVisibleLimit += 24;
      applyPeopleFilterAndSort();
    }});
    peopleFilter?.addEventListener("input", applyPeopleFilterAndSort);
    peopleSort?.addEventListener("change", applyPeopleFilterAndSort);
    refreshPeopleCounts();
    applyPeopleFilterAndSort();
    peopleMergeButton?.addEventListener("click", async () => {{
      const rows = selectedPersonRows();
      const targetId = Number(peopleMergeTarget?.value || 0);
      const sourceIds = rows.map((row) => Number(row.dataset.personId || 0)).filter((id) => id && id !== targetId);
      if (!targetId || sourceIds.length < 1) return;
      const targetLabel = peopleMergeTarget?.selectedOptions?.[0]?.textContent || `person-${{targetId}}`;
      if (!window.confirm(`${{sourceIds.length}} person group(s) will be merged into ${{targetLabel}}. Continue?`)) return;
      localStorage.setItem(peopleManagerOpenStorageKey, "true");
      if (peopleManagerCard) peopleManagerCard.open = true;
      if (peopleMergeState) peopleMergeState.textContent = "병합 중...";
      setPeopleMergeBusy(true);
      try {{
        const response = await fetch("/people/merge", {{
          method: "POST",
          headers: {{"content-type": "application/json"}},
          body: JSON.stringify({{target_person_id: targetId, source_person_ids: sourceIds}}),
        }});
        if (!response.ok) throw new Error(await response.text());
        if (peopleMergeState) peopleMergeState.textContent = "병합 완료. 목록을 새로고침합니다...";
        window.setTimeout(() => window.location.reload(), 350);
      }} catch (error) {{
        if (peopleMergeState) peopleMergeState.textContent = "병합 실패";
        setPeopleMergeBusy(false);
      }}
    }});
    function getAliasesFromForm(form) {{
      const input = form.querySelector("input[name='aliases']");
      if (!input) return [];
      return input.value.split(",").map((v) => v.trim()).filter(Boolean);
    }}
    document.querySelectorAll(".person-row").forEach((form) => {{
      function extractErrorMessage(raw) {{
        if (!raw) return "오류";
        try {{
          const payload = JSON.parse(raw);
          if (typeof payload?.detail === "string" && payload.detail.trim()) return payload.detail.trim();
          if (Array.isArray(payload?.detail) && payload.detail.length) {{
            const first = payload.detail[0];
            if (typeof first?.msg === "string" && first.msg.trim()) return first.msg.trim();
          }}
        }} catch (_error) {{}}
        const text = String(raw).trim();
        return text || "오류";
      }}
      form.addEventListener("submit", async (event) => {{
        event.preventDefault();
        const personId = form.dataset.personId;
        const state = form.querySelector(".person-save-state");
        const button = form.querySelector("button[type='submit']");
        const displayName = form.querySelector("input[name='display_name']").value.trim();
        const aliases = getAliasesFromForm(form);
        if (!displayName && aliases.length === 0) {{
          if (state) state.textContent = "이름 필요";
          return;
        }}
        if (state) state.textContent = "저장 중";
        if (button) button.disabled = true;
        isPeopleSaveInProgress = true;
        pauseFaceSampleLoading();
        try {{
          const response = await fetch(`/people/${{personId}}`, {{
            method: "PATCH",
            headers: {{"content-type": "application/json"}},
            body: JSON.stringify({{display_name: displayName, aliases}}),
          }});
          if (!response.ok) throw new Error(extractErrorMessage(await response.text()));
          if (state) state.textContent = "저장됨";
          const payload = await response.json();
          const savedName = String(payload?.display_name || displayName).trim();
          const savedAliases = Array.isArray(payload?.aliases) ? payload.aliases.map((value) => String(value).trim()).filter(Boolean) : [];
          const isNamed = !!savedName && !savedName.startsWith("person-");
          form.dataset.personLabel = savedName;
          form.dataset.named = isNamed ? "1" : "0";
          form.dataset.personSearch = [savedName, savedAliases.join(", "), `person-${{String(personId).padStart(6, "0")}}`].join(" ").toLowerCase();
          form.querySelectorAll(".person-preview-trigger").forEach((previewButton) => {{
            previewButton.dataset.personLabel = savedName;
          }});
          const displayNameInput = form.querySelector("input[name='display_name']");
          if (displayNameInput) {{
            displayNameInput.value = isNamed ? savedName : "";
            displayNameInput.placeholder = isNamed ? "이름 입력" : "대표 이름 입력";
          }}
          const aliasInput = form.querySelector("input[name='aliases']");
          if (aliasInput) {{
            aliasInput.value = savedAliases.join(", ");
          }}
          const title = form.querySelector(".person-title");
          if (title) title.textContent = isNamed ? savedName : "이름 없음";
          const titleBadge = form.querySelector(".person-status-badge");
          if (titleBadge) {{
            titleBadge.textContent = isNamed ? "이름 있음" : "이름 필요";
            titleBadge.classList.toggle("named", isNamed);
            titleBadge.classList.toggle("unnamed", !isNamed);
          }}
          const helper = form.querySelector(".person-row-hint");
          if (helper) helper.textContent = isNamed ? "검색에 쓸 이름과 애칭을 함께 관리합니다." : "대표 이름을 비워 두고 애칭만 적으면 첫 애칭이 대표 이름으로 저장됩니다.";
          const aliasChipsContainer = form.querySelector(".alias-chips-container");
          if (aliasChipsContainer) {{
            aliasChipsContainer.innerHTML = savedAliases.length
              ? savedAliases.map((alias) => `<span class="alias-chip">${{escapeHtml(alias)}}<button type="button" class="alias-remove" data-alias="${{escapeHtml(alias)}}" title="${{escapeHtml(alias)}} 제거">×</button></span>`).join("")
              : '<span class="alias-chips-empty-hint">병합 시 여기에 표시</span>';
            aliasChipsContainer.classList.toggle("empty", savedAliases.length === 0);
          }}
          form.classList.toggle("has-aliases", savedAliases.length > 0);
          form.classList.toggle("unnamed", !isNamed);
          if (button) button.textContent = isNamed ? "저장" : "이름 저장";
          refreshPeopleCounts();
          peopleVisibleLimit = Math.max(peopleVisibleLimit, 24);
          applyPeopleFilterAndSort();
        }} catch (error) {{
          if (state) state.textContent = error instanceof Error ? error.message : "오류";
        }} finally {{
          isPeopleSaveInProgress = false;
          queueFaceSampleLoading();
          if (button) button.disabled = false;
        }}
      }});
      // Alias chip removal
      form.addEventListener("click", (e) => {{
        const removeBtn = e.target.closest(".alias-remove");
        if (!removeBtn) return;
        const chip = removeBtn.closest(".alias-chip");
        const container = removeBtn.closest(".alias-chips-container");
        const aliasInput = form.querySelector("input[name='aliases']");
        if (!chip) return;
        const alias = removeBtn.dataset.alias || "";
        chip.remove();
        if (aliasInput) {{
          const updated = aliasInput.value.split(",").map((v) => v.trim()).filter((v) => v && v !== alias);
          aliasInput.value = updated.join(", ");
        }}
        if (container && container.querySelectorAll(".alias-chip").length === 0) {{
          container.classList.add("empty");
          container.innerHTML = '<span class="alias-chips-empty-hint">병합 시 여기에 표시</span>';
          if (aliasInput) aliasInput.value = "";
          form.classList.remove("has-aliases");
        }}
      }});
    }});
    function jobKindLabel(kind) {{
      if (kind === "scan") return "사진 가져오기";
      if (kind === "semantic_backfill" || kind === "semantic_maintenance") return "검색 개선";
      return "사진첩 작업";
    }}
    function jobWorkLabel(kind) {{
      if (kind === "scan") return "사진 가져오기";
      if (kind === "semantic_maintenance") return "검색 갱신";
      if (kind === "semantic_backfill") return "AI 분석";
      return "사진첩 작업";
    }}
    function scheduleLabel(hours) {{
      return hours === null || hours === undefined ? "꺼짐" : `${{hours}}시간`;
    }}
    function activeJobId(job) {{
      return job?.job_id || job?.id || "";
    }}
    function compactProgress(job) {{
      const progress = job?.result?.progress || {{}};
      if (!job || !["queued", "running"].includes(job.status || "")) return "—";
      if (job?.job_kind === "scan") {{
        if (progress.stage === "semantic_maintenance") {{
          return `분석 완료 ${{progress.total_succeeded ?? progress.succeeded ?? 0}} · 검색 ${{progress.total_search_documents_updated ?? progress.search_documents_updated ?? 0}}`;
        }}
        if (progress.stage === "discovering_files") {{
          return `파일 탐색 중 · ${{progress.files_found ?? 0}}개 발견`;
        }}
        if (progress.scan?.total !== undefined) {{
          const pct = progress.scan.total > 0 ? Math.round((progress.scan.current ?? 0) / progress.scan.total * 100) : 0;
          return `${{progress.scan.current ?? 0}} / ${{progress.scan.total}} (${{pct}}%) · 발견 ${{progress.files_found ?? progress.scan.total}}`;
        }}
        if (progress.processed?.total !== undefined) {{
          return `${{progress.processed.current ?? 0}} / ${{progress.processed.total}}`;
        }}
        if (progress.summary?.scanned !== undefined) {{
          return `발견 ${{progress.summary.scanned}}`;
        }}
        return progress.stage || "작업 중";
      }}
      const totalDone = progress.total_succeeded ?? progress.succeeded;
      const totalFailed = progress.total_failed ?? progress.failed;
      const indexed = progress.total_search_documents_updated ?? progress.search_documents_updated;
      const chunk = progress.chunk !== undefined ? `묶음 ${{progress.chunk}} · ` : "";
      if (totalDone !== undefined || indexed !== undefined) {{
        return `${{chunk}}완료 ${{totalDone ?? 0}} · 실패 ${{totalFailed ?? 0}} · 검색 ${{indexed ?? 0}}`;
      }}
      if (progress.current !== undefined) {{
        return `${{chunk}}${{progress.current}} / ${{progress.pending ?? progress.current}}`;
      }}
      return progress.stage || progress.message || "작업 중";
    }}
    function detailedProgress(job) {{
      const progress = job?.result?.progress || {{}};
      if (!job || !["queued", "running"].includes(job.status || "")) return "대기 중";
      if (job.job_kind === "scan") {{
        if (progress.stage === "semantic_maintenance") {{
          const parts = ["검색 분석 중"];
          if (progress.chunk !== undefined) parts.push(`묶음 ${{progress.chunk}}`);
          parts.push(`완료 ${{progress.total_succeeded ?? progress.succeeded ?? 0}}`);
          parts.push(`실패 ${{progress.total_failed ?? progress.failed ?? 0}}`);
          parts.push(`검색 +${{progress.total_search_documents_updated ?? progress.search_documents_updated ?? 0}}`);
          parts.push(`태그 +${{progress.total_auto_tag_values ?? progress.auto_tag_values ?? 0}}`);
          return parts.join(" · ");
        }}
        if (progress.stage === "discovering_files") {{
          const rootIdx = progress.source_root_index ?? "?";
          const rootTotal = progress.source_root_total ?? "?";
          const found = progress.files_found ?? 0;
          const msg = progress.message || "";
          return `파일 탐색 중 (${{rootIdx}}/${{rootTotal}} 경로) · ${{found}}개 발견 · ${{msg}}`;
        }}
        const scan = progress.scan || {{}};
        if (scan.total !== undefined) {{
          const pct = scan.total > 0 ? Math.round((scan.current ?? 0) / scan.total * 100) : 0;
          return `동기화 중 · ${{scan.current ?? 0}} / ${{scan.total}} (${{pct}}%) · 발견 ${{progress.files_found ?? scan.total}} · 실패 ${{scan.failed ?? 0}}`;
        }}
        const processed = progress.processed || {{}};
        if (processed.total !== undefined) {{
          return `처리 중 · ${{processed.current ?? 0}} / ${{processed.total}} · 완료 ${{processed.succeeded ?? 0}} · 실패 ${{processed.failed ?? 0}}`;
        }}
        const summary = progress.summary || {{}};
        if (summary.scanned !== undefined) {{
          return `스캔 중 · 발견 ${{summary.scanned}} · 실패 ${{summary.failed ?? 0}}`;
        }}
        return `작업 중 · ${{progress.stage || progress.message || ""}}`;
      }}
      const parts = ["검색 분석 중"];
      if (progress.chunk !== undefined) parts.push(`묶음 ${{progress.chunk}}`);
      if (progress.pending !== undefined || progress.current !== undefined) {{
        parts.push(`${{progress.current ?? 0}} / ${{progress.pending ?? progress.current ?? 0}}`);
      }}
      parts.push(`완료 ${{progress.total_succeeded ?? progress.succeeded ?? 0}}`);
      parts.push(`실패 ${{progress.total_failed ?? progress.failed ?? 0}}`);
      parts.push(`AI +${{progress.total_embeddings_created ?? progress.embeddings_created ?? 0}}`);
      parts.push(`태그 +${{progress.total_auto_tag_values ?? progress.auto_tag_values ?? 0}}`);
      return parts.join(" · ");
    }}
    function phaseStateLabel(state) {{
      return {{"RUNNING": "실행 중", "QUEUED": "대기열", "WAITING": "대기", "IDLE": "대기 중", "FAILED": "실패", "DONE": "완료", "COMPLETED": "완료"}}[state] || state;
    }}
    function setPhaseState(phase, state, running) {{
      const label = phaseStateLabel(state);
      const badge = document.getElementById(`${{phase}}-state-badge`);
      const text = document.getElementById(`${{phase}}-state-text`);
      const panel = document.getElementById(`${{phase}}-live-panel`);
      if (badge) {{
        badge.textContent = label;
        badge.classList.toggle("is-running", running);
      }}
      if (text) {{
        text.textContent = label;
        text.className = running ? "status-running" : (state === "WAITING" ? "status-warn" : "status-idle");
      }}
      if (panel) {{
        panel.className = running ? "live-panel is-running" : (state === "WAITING" ? "live-panel is-waiting" : "live-panel");
      }}
    }}
    function updateAiMetricState(payload, phase1OwnsActive, phase2OwnsActive) {{
      const sched = payload?.scheduler || {{}};
      const perf = payload?.performance || {{}};
      const cov = payload?.semantic?.coverage || {{}};
      const pending = Number(perf?.ai_summary?.remaining_clip ?? cov.remaining_for_clip ?? 0);
      const backgroundActive = ["semantic_backfill", "semantic_maintenance"].includes(sched.background_task_kind) && sched.background_task_state === "running";
      let label = "완료";
      let className = "metric-state-badge is-idle";
      let note = "현재 이미지 AI 대상 사진은 모두 완료됐습니다.";
      if (phase2OwnsActive || backgroundActive) {{
        label = "진행 중";
        className = "metric-state-badge is-running";
        note = sched.background_task_message || (phase2OwnsActive ? detailedProgress(activeLibraryJob) : "이미지 AI 분석 중");
      }} else if (phase1OwnsActive) {{
        label = "대기";
        className = "metric-state-badge is-waiting";
        note = "동기화가 끝나면 이미지 AI가 이어서 처리됩니다.";
      }} else if (pending > 0) {{
        label = "진행 중";
        className = "metric-state-badge is-running";
        note = `남은 ${{pending}}개가 있어 이미지 AI 자동 처리 대상입니다. 전체 동기화 중이 아니면 백그라운드에서 순차 처리합니다. 다음 확인: ${{sched.next_semantic_maintenance_at || "자동 주기"}}`;
      }}
      const badge = document.getElementById("m-ai-state");
      if (badge) {{
        badge.textContent = label;
        badge.className = className;
      }}
      _setText("m-ai-state-note", note);
    }}
    function renderLibraryJob(job) {{
      if (job?.job_kind === "semantic_backfill" || job?.job_kind === "semantic_maintenance") {{
        return renderSemanticJob(job);
      }}
      return renderScanJob(job);
    }}
    function updateLibraryJobGuards() {{
      const active = activeLibraryJob;
      const hasActive = !!active && ["queued", "running"].includes(active.status || "");
      const phase2OwnsActive = hasActive && (active.job_kind === "semantic_backfill" || active.job_kind === "semantic_maintenance");
      const phase1OwnsActive = hasActive && !phase2OwnsActive;

      scanCard.classList.toggle("is-running", phase1OwnsActive);
      semanticCard.classList.toggle("is-running", phase2OwnsActive);
      scanButton.disabled = phase1OwnsActive;
      if (phase1RetryButton) phase1RetryButton.disabled = phase1OwnsActive || phase2OwnsActive;
      semanticButton.disabled = phase1OwnsActive || phase2OwnsActive;
      semanticCancelButton.style.display = phase2OwnsActive ? "" : "none";
      setPhaseState("phase1", phase1OwnsActive ? "RUNNING" : "IDLE", phase1OwnsActive);
      setPhaseState("phase2", phase2OwnsActive ? "RUNNING" : (phase1OwnsActive ? "WAITING" : "IDLE"), phase2OwnsActive);

      _setText("p1-current-job", phase1OwnsActive ? `${{jobWorkLabel(active.job_kind)}} · ${{activeJobId(active)}}` : "대기 중");
      _setText("p1-progress", phase1OwnsActive ? compactProgress(active) : "—");
      _setText("p2-current-job", phase2OwnsActive ? `${{jobWorkLabel(active.job_kind)}} · ${{activeJobId(active)}}` : "대기 중");
      _setText("p2-progress", phase2OwnsActive ? compactProgress(active) : "—");
      _setText("phase1-live-detail", phase1OwnsActive ? detailedProgress(active) : "대기 중");
      _setText("phase2-live-detail", phase2OwnsActive ? detailedProgress(active) : (phase1OwnsActive ? "사진 가져오기가 끝날 때까지 대기 중" : "대기 중"));
      updateAiMetricState({{ scheduler: schedulerSnapshot, performance: performanceSnapshot, semantic: {{ coverage: semanticCoverageSnapshot }} }}, phase1OwnsActive, phase2OwnsActive);

      if (phase1OwnsActive) {{
        scanResult.classList.add("visible");
        scanResult.textContent = renderLibraryJob(active);
      }}

      if (phase2OwnsActive) {{
        semanticResult.classList.add("visible");
        semanticResult.textContent = renderSemanticJob(active);
      }} else if (phase1OwnsActive) {{
        semanticResult.classList.add("visible");
        semanticResult.textContent = "사진 가져오기가 실행 중입니다. 검색 개선은 끝난 뒤 실행됩니다.";
      }}
    }}
    function _setText(id, text) {{
      const el = document.getElementById(id);
      if (el !== null) el.textContent = text;
    }}

    // ── Detail popup ──────────────────────────────────────────────────────
    const _TAG_TYPE_KO = {{
      auto_object:"사물", auto_scene:"장면", auto_person:"인물", auto_screen:"화면",
      place:"지역", place_detail:"세부 지역", location:"위치", geo:"좌표", geo_detail:"좌표 상세",
      person:"사람",
    }};
    async function openDetailPopup(category, title) {{
      const overlay = document.getElementById("detail-overlay");
      const body = document.getElementById("detail-body");
      const titleEl = document.getElementById("detail-title");
      titleEl.textContent = title;
      body.innerHTML = '<div class="detail-loading">불러오는 중...</div>';
      overlay.classList.add("open");
      document.body.style.overflow = "hidden";
      try {{
        const res = await fetch(`/status/detail/${{category}}`, {{ cache: "no-store" }});
        const data = await res.json();
        renderDetailItems(body, data);
      }} catch (e) {{
        body.innerHTML = '<div class="detail-loading">불러오기 실패</div>';
      }}
    }}
    function closeDetailPopup() {{
      document.getElementById("detail-overlay").classList.remove("open");
      document.body.style.overflow = "";
    }}
    document.addEventListener("keydown", e => {{ if (e.key === "Escape") closeDetailPopup(); }});
    function renderDetailItems(container, data) {{
      const hasSummary = Array.isArray(data.summary) && data.summary.length > 0;
      const hasNotes = Array.isArray(data.notes) && data.notes.length > 0;
      const hasItems = Array.isArray(data.items) && data.items.length > 0;
      if (!hasSummary && !hasNotes && !hasItems) {{
        container.innerHTML = '<div class="detail-loading">데이터 없음</div>';
        return;
      }}
      let html = "";
      if (hasSummary) {{
        html += '<div class="detail-summary-grid">';
        for (const item of data.summary) {{
          const sub = item.sublabel ? `<small>${{escapeHtml(item.sublabel)}}</small>` : "";
          html += `<div class="detail-summary-card"><strong>${{escapeHtml(item.label)}} · ${{Number(item.count || 0).toLocaleString()}}</strong>${{sub}}</div>`;
        }}
        html += '</div>';
      }}
      if (hasNotes) {{
        html += '<ul class="detail-notes">';
        for (const note of data.notes) {{
          html += `<li>${{escapeHtml(note)}}</li>`;
        }}
        html += '</ul>';
      }}
      if (hasItems) {{
        let lastSection = null;
        for (const item of data.items) {{
          if (item.section && item.section !== lastSection) {{
            html += `<div class="detail-section-head">${{escapeHtml(item.section)}}</div>`;
            lastSection = item.section;
          }}
          const sub = item.sublabel ? `<span class="detail-item-sub">${{escapeHtml(item.sublabel)}}</span>` : "";
          html += `<div class="detail-item">
            <span class="detail-item-label">${{escapeHtml(item.label)}}</span>${{sub}}
            <span class="detail-item-count">${{Number(item.count || 0).toLocaleString()}}</span>
          </div>`;
        }}
      }}
      container.innerHTML = html;
    }}
    // ─────────────────────────────────────────────────────────────────────
    async function refreshDashboardStatus() {{
      if (isPeopleMergeInProgress || isPeopleSaveInProgress) return;
      try {{
        const response = await fetch("/status", {{ cache: "no-store" }});
        const payload = await response.json();
        if (!response.ok) return;
        activeLibraryJob = payload?.jobs?.active_library_job || null;
        const sched = payload?.scheduler || {{}};
        const cov = payload?.semantic?.coverage || {{}};
        const perf = payload?.performance || {{}};
        schedulerSnapshot = sched;
        performanceSnapshot = perf;
        semanticCoverageSnapshot = cov;
        const cat = payload?.catalog || {{}};
        const health = payload?.health || {{}};
        const storage = payload?.storage || {{}};

        // NAS 연결 상태 뱃지
        const nasStatus = storage.nas_status || {{}};
        const nasEl = document.getElementById("nas-status-badge");
        if (nasEl) {{
          const roots = Object.keys(nasStatus);
          if (roots.length === 0) {{
            nasEl.textContent = "NAS 확인 중…";
            nasEl.className = "nas-badge nas-unknown";
          }} else {{
            const allOk = roots.every(r => nasStatus[r]);
            const anyOk = roots.some(r => nasStatus[r]);
            if (allOk) {{
              nasEl.textContent = "NAS 연결됨";
              nasEl.className = "nas-badge nas-ok";
            }} else if (anyOk) {{
              nasEl.textContent = "NAS 일부 연결";
              nasEl.className = "nas-badge nas-warn";
            }} else {{
              nasEl.textContent = "NAS 연결 끊김";
              nasEl.className = "nas-badge nas-error";
            }}
          }}
        }}

        if (phase1ScheduleButton) phase1ScheduleButton.innerHTML = `<strong>Auto-run</strong> ${{scheduleLabel(sched.library_interval_hours)}}`;

        // Impact metrics
        if (cat.breakdown?.total !== undefined) _setText("m-total", cat.breakdown.total + "개");
        else if (cat.total !== undefined) _setText("m-total", cat.total + "개");
        if (cat.kind_counts !== undefined) _setText("m-total-kinds", "사진 " + (cat.kind_counts.image || 0) + "개");
        if (cat.breakdown?.summary_text !== undefined) _setText("m-total-status", cat.breakdown.summary_text);
        else if (cat.status_counts !== undefined) _setText("m-total-status", "완료 " + ((cat.status_counts.thumb_done || 0) + (cat.status_counts.analysis_done || 0)) + "개 · 예정 " + ((cat.status_counts.metadata_done || 0) + (cat.status_counts.active || 0)) + "개 · 오류 " + (cat.status_counts.error || 0) + "개");
        if (perf.generated_tags !== undefined) _setText("m-tags", perf.generated_tags);
        if (perf.tagged_media !== undefined) _setText("m-tags-note", perf.tagged_media + "개 사진에 적용");
        if (perf.place_tagged_media !== undefined) _setText("m-places", perf.place_tagged_media);
        if (perf.person_count !== undefined) _setText("m-people", perf.person_count);
        if (perf.people_media !== undefined) _setText("m-people-note", perf.people_media + "개 사진에서 얼굴 감지");
        if (perf.clip_embeddings !== undefined) _setText("m-ai", perf.clip_embeddings + " / " + perf.eligible_media);
        if (perf.ai_summary?.summary_text !== undefined) _setText("m-ai-note", perf.ai_summary.summary_text);
        else if (perf.ai_summary?.note_text !== undefined) _setText("m-ai-note", perf.ai_summary.note_text);
        else if (perf.clip_coverage_percent !== undefined) _setText("m-ai-note", perf.clip_coverage_percent + "% 완료 · " + perf.remaining_clip + "개 남음");

        // Phase 1 card rows
        if (sched.last_poll_at !== undefined) _setText("p1-last-poll", sched.last_poll_at ?? "—");
        if (sched.next_poll_at !== undefined) _setText("p1-next-poll", sched.next_poll_at ?? "—");
        if (sched.last_library_run_at !== undefined) _setText("p1-last-scan", sched.last_library_run_at ?? "—");
        if (sched.next_library_run_at !== undefined) _setText("p1-next-scan", sched.next_library_run_at ?? "—");
        if (cov.search_current !== undefined) _setText("p1-search", cov.search_current + " / " + cov.eligible_media);
        if (cov.remaining_for_search !== undefined) _setText("p1-pending", (cov.remaining_for_search + cov.remaining_for_clip).toString());
        const missingEl = document.getElementById("p1-missing");
        if (missingEl && health.missing !== undefined) {{
          missingEl.className = health.missing ? "status-warn" : "";
          missingEl.textContent = health.missing + (health.missing ? " — re-run Scan Now to attempt re-detection" : "");
        }}

        // Phase 2 card rows
        if (sched.last_semantic_maintenance_at !== undefined) _setText("p2-last-run", sched.last_semantic_maintenance_at ?? "—");
        if (sched.next_semantic_maintenance_at !== undefined) _setText("p2-next-run", sched.next_semantic_maintenance_at ?? "—");
        if (cov.eligible_media !== undefined) _setText("p2-eligible", cov.eligible_media);
        if (cov.clip_embeddings_current !== undefined) _setText("p2-clip", cov.clip_embeddings_current);
        if (cov.auto_tag_states_current !== undefined) _setText("p2-auto-tags", cov.auto_tag_states_current);
        if (cov.search_current !== undefined) _setText("p2-search", cov.search_current + " / " + cov.eligible_media);
        if (cov.remaining_for_search !== undefined) _setText("p2-pending", (cov.remaining_for_search + cov.remaining_for_clip).toString());
        if (cov.remaining_for_face_reanalysis !== undefined) _setText("p2-face-pending", cov.remaining_for_face_reanalysis);
        if (cov.semantic_job_errors !== undefined) _setText("p2-errors", cov.semantic_job_errors);

        updateLibraryJobGuards();
      }} catch (_error) {{}}
    }}
    async function cycleSchedule(phase, button) {{
      if (!button) return;
      button.disabled = true;
      try {{
        const response = await fetch(`/scheduler/cycle/${{phase}}`, {{ method: "POST" }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        const scheduler = payload.scheduler || {{}};
        if (phase === "library" || phase === "phase1") {{
          button.innerHTML = `<strong>Auto-run</strong> ${{scheduleLabel(scheduler.library_interval_hours ?? scheduler.phase1_interval_hours)}}`;
        }} else {{
          button.innerHTML = `<strong>Auto-run</strong> ${{scheduleLabel(scheduler.phase2_interval_hours)}}`;
        }}
        await refreshDashboardStatus();
      }} catch (error) {{
        const target = phase === "library" || phase === "phase1" ? scanResult : semanticResult;
        target.classList.add("visible");
        target.textContent = `error: ${{error.message}}`;
      }} finally {{
        button.disabled = false;
      }}
    }}
    function formatElapsed(startedAt, finishedAt) {{
      if (!startedAt) return "";
      const start = new Date(startedAt);
      const end = finishedAt ? new Date(finishedAt) : new Date();
      const seconds = Math.max(0, Math.floor((end - start) / 1000));
      const minutes = Math.floor(seconds / 60);
      const remain = seconds % 60;
      return minutes > 0 ? `${{minutes}}m ${{remain}}s` : `${{remain}}s`;
    }}
    function renderScanJob(job) {{
      const retryOnly = !!(job?.payload?.retry_errors_only || job?.result?.retry_errors_only || job?.result?.progress?.retry_errors_only);
      const summary = job?.result?.summary || {{}};
      const processed = job?.result?.processed || {{}};
      const progress = job?.result?.progress || {{}};
      const lines = [`상태: ${{phaseStateLabel((job?.status || "").toUpperCase()) || job?.status || "확인 중"}}`];
      if (retryOnly) lines.push("기존 오류 항목을 다시 처리하는 중입니다.");
      if (job?.status === "queued" || job?.status === "running") {{
        if (progress.message) lines.push(progress.message);
        if (progress.stage) lines.push(`단계: ${{progress.stage}}`);
        if (progress.files_found !== undefined) lines.push(`찾은 파일: ${{progress.files_found}}개`);
        if (progress.current_path) lines.push(`현재 파일: ${{progress.current_path}}`);
        if (progress.scan?.total !== undefined) {{
          lines.push(`스캔: ${{progress.scan.current ?? 0}} / ${{progress.scan.total}}`);
          lines.push(`스캔 실패: ${{progress.scan.failed ?? 0}}`);
        }}
        if (!retryOnly && progress.summary?.scanned !== undefined) lines.push(`발견: ${{progress.summary.scanned}}`);
        if (progress.processed?.total !== undefined) {{
          lines.push(`처리: ${{progress.processed.current ?? 0}} / ${{progress.processed.total}}`);
          lines.push(`완료: ${{progress.processed.succeeded ?? 0}}, 실패: ${{progress.processed.failed ?? 0}}`);
        }}
        const elapsed = formatElapsed(job?.started_at, job?.finished_at);
        if (elapsed) lines.push(`소요 시간: ${{elapsed}}`);
        return lines.join("\\n");
      }}
      if (progress.message) lines.push(progress.message);
      if (!retryOnly) {{
        lines.push(
          `발견: ${{summary.scanned ?? 0}}`,
          `새로 추가: ${{summary.created ?? 0}}`,
          `업데이트: ${{summary.updated ?? 0}}`,
          `이동 감지: ${{summary.moved ?? 0}}`,
          `누락: ${{summary.missing ?? 0}}`,
          `실패: ${{summary.failed ?? 0}}`,
        );
      }}
      lines.push(`처리 완료: ${{processed.succeeded ?? 0}}, 실패: ${{processed.failed ?? 0}}`);
      const semantic = job?.result?.semantic || {{}};
      if (semantic.search_documents_updated !== undefined) lines.push(`검색 색인: +${{semantic.search_documents_updated}}`);
      if (semantic.auto_tag_files !== undefined) lines.push(`자동 태그: ${{semantic.auto_tag_files}} 항목, +${{semantic.auto_tag_values ?? 0}}개`);
      if (semantic.faces_reanalyzed) lines.push(`얼굴 재분석: +${{semantic.faces_reanalyzed}}`);
      if (semantic.embeddings_created) lines.push(`AI 임베딩: +${{semantic.embeddings_created}}`);
      const elapsed = formatElapsed(job?.started_at, job?.finished_at);
      if (elapsed) lines.push(`소요 시간: ${{elapsed}}`);
      if (job?.error_message) lines.push(`오류: ${{job.error_message}}`);
      return lines.join("\\n");
    }}
    async function pollJob(jobId, resultNode, render) {{
      while (true) {{
        const response = await fetch(`/scan/jobs/${{encodeURIComponent(jobId)}}`, {{ cache: "no-store" }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        const job = payload.job;
        resultNode.textContent = render(job);
        if (job.status !== "queued" && job.status !== "running") return job;
        await sleep(1200);
      }}
    }}
    function rememberJob(key, jobId) {{
      try {{
        localStorage.setItem(key, jobId);
      }} catch (_error) {{}}
    }}
    function forgetJob(key) {{
      try {{
        localStorage.removeItem(key);
      }} catch (_error) {{}}
    }}
    function loadRememberedJob(key) {{
      try {{
        return localStorage.getItem(key) || "";
      }} catch (_error) {{
        return "";
      }}
    }}
    function rememberText(key, value) {{
      try {{
        localStorage.setItem(key, value);
      }} catch (_error) {{}}
    }}
    function loadRememberedText(key) {{
      try {{
        return localStorage.getItem(key) || "";
      }} catch (_error) {{
        return "";
      }}
    }}
    function appendSourceRoot(path) {{
      if (!sourceRootsField || !path) return;
      const existing = sourceRootsField.value
        .split(/\\n+/)
        .map((value) => value.trim())
        .filter(Boolean);
      if (!existing.includes(path)) existing.push(path);
      sourceRootsField.value = existing.join("\\n");
      rememberText(phase1SourceRootsStorageKey, sourceRootsField.value);
    }}
    async function loadSourcePicker(path = "") {{
      if (!sourcePicker || !sourcePickerList) return;
      const params = new URLSearchParams();
      if (path) params.set("path", path);
      sourcePickerList.innerHTML = `<div class="source-picker-item"><strong>불러오는 중...</strong><small></small><span></span></div>`;
      try {{
        const response = await fetch(`/source-roots/browse?${{params.toString()}}`, {{ cache: "no-store" }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        if (sourcePickerPath) sourcePickerPath.textContent = payload.path || "바로가기";
        if (sourcePickerNote) sourcePickerNote.textContent = payload.note || "";
        if (sourcePickerUp) {{
          sourcePickerUp.disabled = !payload.parent;
          sourcePickerUp.dataset.path = payload.parent || "";
        }}
        const entries = payload.entries || [];
        sourcePickerList.innerHTML = "";
        if (!entries.length) {{
          sourcePickerList.innerHTML = `<div class="source-picker-item"><strong>여기에서 보이는 폴더가 없습니다.</strong><small></small><span></span></div>`;
          return;
        }}
        entries.forEach((entry) => {{
          const row = document.createElement("div");
          row.className = "source-picker-item";

          const label = document.createElement("div");
          const name = document.createElement("strong");
          name.textContent = entry.name || entry.path;
          const detail = document.createElement("small");
          detail.textContent = entry.path || "";
          label.append(name, detail);

          const openButton = document.createElement("button");
          openButton.type = "button";
          openButton.textContent = "열기";
          openButton.addEventListener("click", () => loadSourcePicker(entry.path));

          const selectButton = document.createElement("button");
          selectButton.type = "button";
          selectButton.className = "primary";
          selectButton.textContent = "선택";
          selectButton.addEventListener("click", () => appendSourceRoot(entry.path));

          row.append(label, openButton, selectButton);
          sourcePickerList.append(row);
        }});
      }} catch (error) {{
        sourcePickerList.innerHTML = `<div class="source-picker-item"><strong>오류: ${{escapeHtml(error.message)}}</strong><small></small><span></span></div>`;
      }}
    }}
    async function resumeJob(key, card, button, result, render) {{
      const jobId = loadRememberedJob(key);
      if (!jobId) return;
      result.classList.add("visible");
      card.classList.add("is-running");
      button.disabled = true;
      result.textContent = "실행 중인 작업을 다시 연결합니다...";
      try {{
        const job = await pollJob(jobId, result, render);
        result.textContent = render(job);
        if (job.status !== "queued" && job.status !== "running") {{
          forgetJob(key);
        }}
      }} catch (error) {{
        result.textContent = `오류: ${{error.message}}`;
        forgetJob(key);
      }} finally {{
        card.classList.remove("is-running");
        button.disabled = false;
      }}
    }}
    function renderSemanticJob(job) {{
      const result = job?.result || {{}};
      const progress = result.progress || {{}};
      const lines = [`상태: ${{phaseStateLabel((job?.status || "").toUpperCase()) || job?.status || "확인 중"}}`];
      if (job?.status === "queued" || job?.status === "running") {{
        if (progress.message) lines.push(progress.message);
        if (progress.mode) lines.push(`방식: ${{progress.mode}}`);
        if (progress.full_run) lines.push(`범위: 전체 사진첩`);
        if (progress.chunk !== undefined) lines.push(`묶음: ${{progress.chunk}}`);
        if (progress.pending !== undefined) lines.push(`대상: ${{progress.pending}}`);
        if (progress.current !== undefined) lines.push(`처리: ${{progress.current}} / ${{progress.pending ?? progress.current}}`);
        if (progress.succeeded !== undefined || progress.failed !== undefined) {{
          lines.push(`완료: ${{progress.succeeded ?? 0}}, 실패: ${{progress.failed ?? 0}}`);
        }}
        if (progress.total_succeeded !== undefined || progress.total_failed !== undefined) {{
          lines.push(`전체 완료: ${{progress.total_succeeded ?? 0}}, 실패: ${{progress.total_failed ?? 0}}`);
        }}
        if (progress.embeddings_created !== undefined) lines.push(`이미지 AI: +${{progress.embeddings_created}}`);
        if (progress.auto_tag_files !== undefined || progress.auto_tag_values !== undefined) {{
          lines.push(`자동 태그: ${{progress.auto_tag_files ?? 0}}개 사진, +${{progress.auto_tag_values ?? 0}}개`);
        }}
        if (progress.search_documents_updated !== undefined) lines.push(`검색 색인: +${{progress.search_documents_updated}}`);
        if (progress.faces_reanalyzed !== undefined) lines.push(`얼굴 분석: +${{progress.faces_reanalyzed}}`);
        if (progress.face_analysis_available === false) lines.push(`얼굴 분석 모델이 준비되지 않았습니다`);
        if (progress.clip_enabled === false) lines.push(`이미지 AI가 꺼져 있습니다`);
        if (progress.total_embeddings_created !== undefined) lines.push(`전체 이미지 AI: +${{progress.total_embeddings_created}}`);
        if (progress.total_auto_tag_files !== undefined || progress.total_auto_tag_values !== undefined) {{
          lines.push(`전체 자동 태그: ${{progress.total_auto_tag_files ?? 0}}개 사진, +${{progress.total_auto_tag_values ?? 0}}개`);
        }}
        if (progress.total_search_documents_updated !== undefined) lines.push(`전체 검색 색인: +${{progress.total_search_documents_updated}}`);
        if (progress.total_faces_reanalyzed !== undefined) lines.push(`전체 얼굴 분석: +${{progress.total_faces_reanalyzed}}`);
        const elapsed = formatElapsed(job?.started_at, job?.finished_at);
        if (elapsed) lines.push(`소요 시간: ${{elapsed}}`);
        return lines.join("\\n");
      }}
      if (progress.message) lines.push(progress.message);
      lines.push(
        `대상: ${{result.pending ?? 0}}`,
        `완료: ${{result.succeeded ?? 0}}`,
        `실패: ${{result.failed ?? 0}}`,
      );
      if (result.full_run) lines.push(`범위: 전체 사진첩`);
      if (result.chunks !== undefined) lines.push(`묶음: ${{result.chunks}}`);
      if (result.embeddings_created !== undefined) lines.push(`이미지 AI: +${{result.embeddings_created}}`);
      if (result.auto_tag_files !== undefined || result.auto_tag_values !== undefined) {{
        lines.push(`자동 태그: ${{result.auto_tag_files ?? 0}}개 사진, +${{result.auto_tag_values ?? 0}}개`);
      }}
      if (result.search_documents_updated !== undefined) lines.push(`검색 색인: +${{result.search_documents_updated}}`);
      if (result.faces_reanalyzed !== undefined) lines.push(`얼굴 분석: +${{result.faces_reanalyzed}}`);
      const elapsed = formatElapsed(job?.started_at, job?.finished_at);
      if (elapsed) lines.push(`소요 시간: ${{elapsed}}`);
      const faceAvail = result.face_analysis_available;
      if (faceAvail === false) lines.push(`얼굴 분석 모델이 준비되지 않았습니다`);
      if (result.clip_enabled === false) lines.push(`이미지 AI가 꺼져 있습니다`);
      if (job?.error_message) lines.push(`오류: ${{job.error_message}}`);
      return lines.join("\\n");
    }}
    const rememberedSourceRoots = loadRememberedText(phase1SourceRootsStorageKey);
    if (sourceRootsField && rememberedSourceRoots.trim()) {{
      sourceRootsField.value = rememberedSourceRoots;
    }}
    updateLibraryJobGuards();
    setInterval(refreshDashboardStatus, 3000);
    phase1ScheduleButton?.addEventListener("click", () => cycleSchedule("library", phase1ScheduleButton));
    sourcePickerOpen?.addEventListener("click", () => {{
      if (!sourcePicker) return;
      sourcePicker.hidden = !sourcePicker.hidden;
      if (!sourcePicker.hidden) loadSourcePicker();
    }});
    sourcePickerHome?.addEventListener("click", () => loadSourcePicker());
    sourcePickerUp?.addEventListener("click", () => loadSourcePicker(sourcePickerUp.dataset.path || ""));
    sourceRootsField?.addEventListener("input", () => {{
      rememberText(phase1SourceRootsStorageKey, sourceRootsField.value);
    }});
    scanForm?.addEventListener("submit", (event) => event.preventDefault());
    semanticForm?.addEventListener("submit", (event) => event.preventDefault());
    scanButton?.addEventListener("click", async () => {{
      if (activeLibraryJob && ["queued", "running"].includes(activeLibraryJob.status || "")) {{
        scanResult.classList.add("visible");
        scanResult.textContent = "다른 라이브러리 작업이 실행 중입니다. 끝난 뒤 다시 시도하세요.";
        return;
      }}
      scanResult.classList.add("visible");
      scanCard.classList.add("is-running");
      scanButton.disabled = true;
      scanResult.textContent = "라이브러리 동기화를 시작합니다...";
      activeLibraryJob = {{ job_kind: "scan", status: "queued" }};
      updateLibraryJobGuards();
      const sourceRoots = sourceRootsField ? sourceRootsField.value : "";
      rememberText(phase1SourceRootsStorageKey, sourceRoots);
      const params = new URLSearchParams();
      if (sourceRoots.trim()) params.set("source_roots", sourceRoots);
      params.set("full_scan", "true");
      try {{
        const response = await fetch(`/scan/async?${{params.toString()}}`, {{ method: "POST" }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        rememberJob(phase1StorageKey, payload.job.job_id);
        scanResult.textContent = renderScanJob(payload.job);
        const job = await pollJob(payload.job.job_id, scanResult, renderScanJob);
        scanResult.textContent = renderScanJob(job);
        if (job.status !== "queued" && job.status !== "running") {{
          forgetJob(phase1StorageKey);
        }}
      }} catch (error) {{
        scanResult.textContent = `error: ${{error.message}}`;
        forgetJob(phase1StorageKey);
      }} finally {{
        scanCard.classList.remove("is-running");
        refreshDashboardStatus();
      }}
    }});
    phase1RetryButton?.addEventListener("click", async () => {{
      if (activeLibraryJob && ["queued", "running"].includes(activeLibraryJob.status || "")) {{
        scanResult.classList.add("visible");
        scanResult.textContent = "다른 작업이 실행 중입니다. 끝난 뒤 다시 시도하세요.";
        return;
      }}
      scanResult.classList.add("visible");
      scanCard.classList.add("is-running");
      phase1RetryButton.disabled = true;
      scanButton.disabled = true;
      scanResult.textContent = "기존 오류 항목을 다시 처리합니다...";
      activeLibraryJob = {{ job_kind: "scan", status: "queued", payload: {{ retry_errors_only: true }} }};
      updateLibraryJobGuards();
      try {{
        const response = await fetch("/scan/retry-errors/async", {{ method: "POST" }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        rememberJob(phase1StorageKey, payload.job.job_id);
        scanResult.textContent = renderScanJob(payload.job);
        const job = await pollJob(payload.job.job_id, scanResult, renderScanJob);
        scanResult.textContent = renderScanJob(job);
        if (job.status !== "queued" && job.status !== "running") {{
          forgetJob(phase1StorageKey);
        }}
      }} catch (error) {{
        scanResult.textContent = `error: ${{error.message}}`;
        forgetJob(phase1StorageKey);
      }} finally {{
        scanCard.classList.remove("is-running");
        refreshDashboardStatus();
      }}
    }});
    semanticButton?.addEventListener("click", async () => {{
      if (activeLibraryJob && ["queued", "running"].includes(activeLibraryJob.status || "") && activeLibraryJob.job_kind !== "semantic_backfill" && activeLibraryJob.job_kind !== "semantic_maintenance") {{
        semanticResult.classList.add("visible");
        semanticResult.textContent = "사진 가져오기가 실행 중입니다. 검색 개선은 끝난 뒤 실행됩니다.";
        return;
      }}
      semanticResult.classList.add("visible");
      semanticCard.classList.add("is-running");
      semanticButton.disabled = true;
      semanticCancelButton.style.display = "";
      semanticResult.textContent = "검색 개선을 시작합니다...";
      const endpoint = "/scan/semantic-maintenance/async";
      activeLibraryJob = {{ job_kind: "semantic_maintenance", status: "queued" }};
      updateLibraryJobGuards();
      try {{
        const response = await fetch(endpoint, {{ method: "POST" }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        rememberJob(phase2StorageKey, payload.job.job_id);
        semanticResult.textContent = renderSemanticJob(payload.job);
        const job = await pollJob(payload.job.job_id, semanticResult, renderSemanticJob);
        semanticResult.textContent = renderSemanticJob(job);
        if (job.status !== "queued" && job.status !== "running") {{
          forgetJob(phase2StorageKey);
        }}
      }} catch (error) {{
        semanticResult.textContent = `error: ${{error.message}}`;
        forgetJob(phase2StorageKey);
      }} finally {{
        semanticCard.classList.remove("is-running");
        semanticCancelButton.style.display = "none";
        refreshDashboardStatus();
      }}
    }});
    semanticCancelButton?.addEventListener("click", async () => {{
      const jobId = loadRememberedJob(phase2StorageKey) || activeLibraryJob?.job_id || activeLibraryJob?.id || "";
      if (!jobId) return;
      semanticCancelButton.disabled = true;
      try {{
        const response = await fetch(`/scan/jobs/${{encodeURIComponent(jobId)}}/cancel`, {{ method: "POST" }});
        const payload = await response.json();
        semanticResult.textContent = `중지를 요청했습니다. 현재 묶음이 끝나면 멈춥니다.\n${{payload.message || ""}}`;
      }} catch (error) {{
        semanticResult.textContent = `중지 오류: ${{error.message}}`;
      }} finally {{
        semanticCancelButton.disabled = false;
      }}
    }});
    resumeJob(phase1StorageKey, scanCard, scanButton, scanResult, renderScanJob);
    resumeJob(phase2StorageKey, semanticCard, semanticButton, semanticResult, renderSemanticJob);
    // Show cancel button if a job is already running from a previous session
    if (loadRememberedJob(phase2StorageKey)) semanticCancelButton.style.display = "";

    const _INTENT_LABELS = {{
      "fallback": "스마트 검색", "date_relaxed": "날짜 범위 확대",
      "fuzzy_corrected": "유사어 보정", "auto-face": "얼굴·인물 검색",
      "auto-travel": "여행 사진 검색", "auto-celebration": "행사 사진 검색",
      "auto-mixed": "복합 검색", "auto-text-hint": "텍스트 검색",
      "auto-screen-text": "화면·캡처 검색", "auto-code": "문서·코드 검색",
      "auto-word-match": "단어 일치", "auto-phrase-code": "구문 일치",
      "planner-ocr": "텍스트 추출", "planner-visual": "이미지 검색",
      "manual": "직접 지정", "condition_visual_only": "키워드 검색",
      "condition_place_only": "장소 검색", "condition_person_only": "인물 검색",
      "empty": "검색어 없음", "degenerate": "검색 불가",
    }};
    function renderSearchMeta(meta) {{
      if (!meta) return "(no result)";
      const plan = meta.query_plan || {{}};
      const reason = meta.intent_reason || "";
      const label = _INTENT_LABELS[reason] || reason;
      const modeLabel = {{ "hybrid": "자동", "ocr": "텍스트", "semantic": "이미지 AI" }}[meta.effective_mode] || meta.effective_mode;
      const lines = [
        `검색 방식: ${{modeLabel}} (${{label}})`,
      ];
      if ((plan.person_terms || []).length) lines.push(`인물: ${{plan.person_terms.join(", ")}}`);
      if ((plan.place_terms || []).length) lines.push(`장소: ${{plan.place_terms.join(", ")}}`);
      if (plan.date_from) lines.push(`날짜: ${{plan.date_from}}${{plan.date_to ? " ~ " + plan.date_to : ""}}`);
      if (meta.fallback) lines.push(`재검색: ${{_INTENT_LABELS[meta.fallback] || meta.fallback}}`);
      if (meta.fuzzy_corrected_query) lines.push(`보정된 검색어: "${{meta.fuzzy_corrected_query}}"`);
      if (Object.keys(meta.weight_overrides || {{}}).length) {{
        const w = meta.weight_overrides;
        lines.push(`가중치 오버라이드: OCR=${{w.ocr ?? "—"}}  AI=${{w.clip ?? "—"}}  키워드=${{w.shadow ?? "—"}}`);
      }}
      lines.push("", "── 원시 데이터 ──");
      lines.push(JSON.stringify(meta, null, 2));
      return lines.join("\\n");
    }}
    const searchDebugForm = document.getElementById("search-debug-form");
    const searchDebugResult = document.getElementById("search-debug-result");
    searchDebugForm?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      searchDebugResult.textContent = "Inspecting search...";
      const params = new URLSearchParams();
      const query = document.getElementById("search-debug-query").value;
      const mode = document.getElementById("search-debug-mode").value;
      const place = document.getElementById("search-debug-place").value;
      const wOcr = document.getElementById("search-debug-w-ocr").value;
      const wClip = document.getElementById("search-debug-w-clip").value;
      const wShadow = document.getElementById("search-debug-w-shadow").value;
      if (query.trim()) params.set("q", query);
      if (mode) params.set("mode", mode);
      if (place.trim()) params.set("place", place);
      if (wOcr.trim()) params.set("w_ocr", wOcr);
      if (wClip.trim()) params.set("w_clip", wClip);
      if (wShadow.trim()) params.set("w_shadow", wShadow);
      try {{
        const response = await fetch(`/search/debug?${{params.toString()}}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        searchDebugResult.textContent = renderSearchMeta(payload.meta);
      }} catch (error) {{
        searchDebugResult.textContent = `error: ${{error.message}}`;
      }}
    }});

    const benchmarkRun = document.getElementById("search-benchmark-run");
    const benchmarkSummary = document.getElementById("search-benchmark-summary");
    const benchmarkResult = document.getElementById("search-benchmark-result");
    benchmarkRun?.addEventListener("click", async () => {{
      benchmarkSummary.textContent = "";
      benchmarkResult.textContent = "Running benchmark...";
      try {{
        const params = new URLSearchParams();
        const wOcr = document.getElementById("search-debug-w-ocr").value;
        const wClip = document.getElementById("search-debug-w-clip").value;
        const wShadow = document.getElementById("search-debug-w-shadow").value;
        if (wOcr.trim()) params.set("w_ocr", wOcr);
        if (wClip.trim()) params.set("w_clip", wClip);
        if (wShadow.trim()) params.set("w_shadow", wShadow);
        const response = await fetch(`/search/benchmark?${{params.toString()}}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `HTTP ${{response.status}}`);
        const overrideText = Object.keys(payload.weight_overrides || {{}}).length
          ? `, overrides ${{JSON.stringify(payload.weight_overrides)}}`
          : "";
        const failedChecks = Object.keys(payload.summary?.failed_checks || {{}}).length
          ? `, failed checks ${{JSON.stringify(payload.summary.failed_checks)}}`
          : "";
        const passIcon = payload.failed === 0 ? "✓" : "✗";
        benchmarkSummary.textContent = `${{passIcon}} ${{payload.passed}} / ${{payload.total}} 통과, ${{payload.failed}} 실패${{overrideText}}${{failedChecks}}`;
        benchmarkResult.textContent = JSON.stringify({{
          summary: payload.summary,
          cases: payload.cases,
        }}, null, 2);
      }} catch (error) {{
        benchmarkResult.textContent = `error: ${{error.message}}`;
      }}
    }});

    // ── AI pack install flow ──────────────────────────────────────────
    function copyText(id, btn) {{
      const text = document.getElementById(id)?.textContent || "";
      navigator.clipboard.writeText(text).then(() => {{
        btn.textContent = "Copied!";
        setTimeout(() => btn.textContent = "복사", 1500);
      }});
    }}

    let _aiPollTimer = null;

    function aiPackPrepare(loadCached = false) {{
      const btn = document.getElementById("ai-download-btn");
      if (btn) {{ btn.disabled = true; btn.textContent = "시작 중..."; }}
      const url = loadCached ? "/ai-pack/prepare?load_cached=true" : "/ai-pack/prepare";
      fetch(url, {{ method: "POST" }})
        .then(r => r.json())
        .then(d => {{
          if (d.ok) {{ startAiPoll(); }} else {{ alert(d.message); if (btn) btn.disabled = false; }}
        }})
        .catch(e => {{ alert("오류: " + e.message); if (btn) btn.disabled = false; }});
    }}

    function startAiPoll() {{
      if (_aiPollTimer) return;
      _aiPollTimer = setInterval(pollAiProgress, 2000);
      pollAiProgress();
    }}

    function pollAiProgress() {{
      fetch("/ai-pack/progress")
        .then(r => r.json())
        .then(data => {{
          const stage = data.stage;
          updateAiStepUI(stage, data);
          if (stage === "ready" || stage === "error") {{
            clearInterval(_aiPollTimer);
            _aiPollTimer = null;
          }}
        }})
        .catch(() => {{ /* ignore transient errors */ }});
    }}

    function updateAiStepUI(stage, data) {{
      const step2 = document.getElementById("ai-step-2");
      const step3 = document.getElementById("ai-step-3");
      if (!step2) return;

      // Update step-2 body
      const body2 = step2.querySelector(".ai-step-body");
      if (stage === "downloading") {{
        step2.className = "ai-step ai-step-active";
        body2.innerHTML = body2.innerHTML.replace(/<button[^>]*>.*?<\\/button>/s, "");
        if (!body2.querySelector(".ai-spinner")) {{
          const lbl = document.getElementById("ai-dl-label");
          if (!lbl) body2.insertAdjacentHTML("beforeend", '<span><span class="ai-spinner"></span> 준비 중...</span>');
        }}
      }} else if (stage === "ready") {{
        step2.className = "ai-step ai-step-done";
        step2.querySelector(".ai-step-num").textContent = "✓";
        body2.querySelectorAll("button,span.ai-spinner,#ai-dl-label").forEach(el => el.remove());
        body2.insertAdjacentHTML("beforeend", '<span class="status-ok">준비됨</span>');
        // Activate step
        if (step3) {{
          step3.className = "ai-step ai-step-active";
          step3.querySelector(".ai-step-num").textContent = "3";
          const body3 = step3.querySelector(".ai-step-body");
          body3.querySelector(".muted")?.remove();
          if (!body3.querySelector(".ai-cmd-row")) {{
            body3.insertAdjacentHTML("beforeend", `<p class="ai-step-desc">아래 환경 변수를 설정하고 재시작하세요.</p>
              <div class="ai-cmd-row">
                <code id="activate-cmd">${{aiActivateCmd}}</code>
                <button class="btn-copy" onclick="copyText('activate-cmd', this)">복사</button>
              </div>`);
          }}
        }}
      }} else if (stage === "error") {{
        step2.className = "ai-step ai-step-active";
        const errMsg = data.model_error || "알 수 없는 오류";
        const offline = {str(settings.offline_mode).lower()};
        const retry = offline ? '<button class="btn-sm" id="ai-download-btn" onclick="aiPackPrepare(true)">로컬 캐시 확인</button>' : '<button class="btn-sm" onclick="aiPackPrepare()">다시 시도</button>';
        if (offline) {{
          body2.innerHTML = `<strong>로컬 모델 캐시 사용</strong>
            <span class="status-warn">마지막 시도: ${{errMsg}}</span>
            ${{retry}}
            <p class="ai-step-desc">이 버튼은 새 모델을 받지 않습니다. 현재 캐시 폴더에 이미 있는 모델을 불러오기만 시도합니다.</p>
            <p class="ai-step-desc">캐시가 비어 있으면 한 번만 온라인 준비 모드로 재시작해서 모델을 받은 뒤, 다시 오프라인 모드로 돌아와야 합니다.</p>
            <div class="ai-cmd-row">
              <code id="online-ai-cmd">${{aiOnlineCmd}}</code>
              <button class="btn-copy" onclick="copyText('online-ai-cmd', this)">복사</button>
            </div>
            <div class="ai-cmd-row">
              <code id="offline-ai-cmd">${{aiOfflineCmd}}</code>
              <button class="btn-copy" onclick="copyText('offline-ai-cmd', this)">복사</button>
            </div>`;
        }} else {{
          body2.innerHTML = `<strong>모델</strong><span class="status-warn">오류: ${{errMsg}}</span>${{retry}}`;
        }}
      }}
    }}

    // 이미지 AI 설치 카드 제거됨 — 진행 폴링 auto-start 비활성.
    // (aiPackPrepare/updateAiStepUI는 null 가드가 있어 호출돼도 무해.)
    // ai pack install card removed

  </script>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    settings = require_state(request, "settings")
    database = require_state(request, "database")
    pipeline = require_state(request, "pipeline")
    scheduler = require_state(request, "scheduler")

    with database.session_factory() as session:
        catalog = MediaCatalog(session)
        pipeline_snapshot = pipeline.status_snapshot()
        clip_model_identifier = f"{settings.semantic_clip_model_name}/{settings.semantic_clip_pretrained}"
        active_media_status = MediaFile.status.not_in(("missing", "replaced", "excluded"))
        media_with_thumb_asset = select(DerivedAsset.file_id).where(DerivedAsset.asset_kind == "thumb").distinct()
        dashboard_ready_status = or_(
            MediaFile.status.in_(("thumb_done", "analysis_done")),
            MediaFile.file_id.in_(media_with_thumb_asset),
        )
        eligible_media_count = int(
            session.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(
                    active_media_status,
                    dashboard_ready_status,
                    MediaFile.media_kind == "image",
                )
            )
            or 0
        )
        clip_embeddings_current = int(
            session.scalar(
                select(func.count())
                .select_from(MediaEmbedding)
                .join(MediaFile, MediaFile.file_id == MediaEmbedding.file_id)
                .where(
                    active_media_status,
                    MediaFile.media_kind == "image",
                    MediaEmbedding.model_name == clip_model_identifier,
                    MediaEmbedding.version == settings.semantic_embedding_version,
                )
            )
            or 0
        )
        auto_tag_states_current = int(
            session.scalar(
                select(func.count())
                .select_from(MediaAutoTagState)
                .join(MediaFile, MediaFile.file_id == MediaAutoTagState.file_id)
                .where(
                    active_media_status,
                    MediaFile.media_kind == "image",
                    MediaAutoTagState.version == settings.semantic_auto_tag_version,
                )
            )
            or 0
        )
        search_documents_current = int(
            session.scalar(
                select(func.count())
                .select_from(SearchDocument)
                .join(MediaFile, MediaFile.file_id == SearchDocument.file_id)
                .where(
                    active_media_status,
                    MediaFile.media_kind == "image",
                    SearchDocument.version == settings.semantic_search_version,
                )
            )
            or 0
        )
        semantic_job_errors = int(
            session.scalar(
                select(func.count())
                .select_from(ProcessingJob)
                .where(
                    ProcessingJob.job_kind.in_(("semantic_backfill", "semantic_maintenance")),
                    ProcessingJob.status == "failed",
                )
            )
            or 0
        )
        face_reanalysis_pending = 0
        if settings.face_analysis_enabled:
            face_reanalysis_pending = int(
                session.scalar(
                    select(func.count())
                    .select_from(MediaFile)
                    .where(
                        MediaFile.status.in_(("thumb_done", "analysis_done")),
                        MediaFile.media_kind == "image",
                        or_(
                            MediaFile.face_version.is_(None),
                            MediaFile.face_version != settings.face_analysis_version,
                        ),
                    )
                )
                or 0
            )
        recent_jobs = session.execute(
            select(ProcessingJob).order_by(ProcessingJob.updated_at.desc(), ProcessingJob.enqueued_at.desc()).limit(10)
        ).scalars().all()
        total_images = int(
            session.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(
                    MediaFile.media_kind == "image",
                    MediaFile.status != "excluded",
                )
            )
            or 0
        )
        completed_media_status = MediaFile.status.in_(("thumb_done", "analysis_done"))
        completed_images = int(
            session.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(completed_media_status, MediaFile.media_kind == "image")
            )
            or 0
        )
        completed_videos = int(
            session.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(completed_media_status, MediaFile.media_kind == "video")
            )
            or 0
        )
        total_tags = int(
            session.scalar(
                select(func.count())
                .select_from(Tag)
                .join(MediaFile, MediaFile.file_id == Tag.file_id)
                .where(active_media_status, MediaFile.media_kind == "image")
            )
            or 0
        )
        tagged_media_count = int(
            session.scalar(
                select(func.count(func.distinct(Tag.file_id)))
                .join(MediaFile, MediaFile.file_id == Tag.file_id)
                .where(active_media_status, MediaFile.media_kind == "image")
            )
            or 0
        )
        place_tagged_media_count = int(
            session.scalar(
                select(func.count(func.distinct(Tag.file_id)))
                .join(MediaFile, MediaFile.file_id == Tag.file_id)
                .where(
                    active_media_status,
                    MediaFile.media_kind == "image",
                    Tag.tag_type.in_(("place", "place_detail", "location", "geo", "geo_detail")),
                )
            )
            or 0
        )
        faces_detected = int(session.scalar(select(func.count()).select_from(Face)) or 0)
        person_count = int(
            session.scalar(
                select(func.count(func.distinct(Face.person_id)))
                .join(MediaFile, MediaFile.file_id == Face.file_id)
                .where(active_media_status, Face.person_id.is_not(None))
            )
            or 0
        )
        people_media_count = int(
            session.scalar(
                select(func.count(func.distinct(Face.file_id)))
                .join(MediaFile, MediaFile.file_id == Face.file_id)
                .where(active_media_status)
            )
            or 0
        )
        people_candidate_filter = or_(
            func.count(Face.id).filter(active_media_status) >= 5,
            Person.display_name.not_like("person-%"),
        )
        people_total_count = int(
            session.scalar(
                select(func.count())
                .select_from(
                    select(Person.id)
                    .outerjoin(Face, Face.person_id == Person.id)
                    .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
                    .group_by(Person.id)
                    .having(people_candidate_filter)
                    .subquery()
                )
            )
            or 0
        )
        people_named_count = int(
            session.scalar(
                select(func.count())
                .select_from(
                    select(Person.id)
                    .outerjoin(Face, Face.person_id == Person.id)
                    .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
                    .group_by(Person.id)
                    .having(people_candidate_filter, Person.display_name.not_like("person-%"))
                    .subquery()
                )
            )
            or 0
        )
        people_loaded_limit = 1000
        people_rows = session.execute(
            select(
                Person,
                func.count(Face.id).filter(active_media_status).label("face_count"),
                func.count(func.distinct(Face.file_id)).filter(active_media_status).label("media_count"),
            )
            .outerjoin(Face, Face.person_id == Person.id)
            .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
            .group_by(Person.id)
            .having(people_candidate_filter)
            .order_by(func.count(Face.id).filter(active_media_status).desc(), Person.id.asc())
            .limit(people_loaded_limit)
        ).all()
        people_payload = []
        for person, face_count, media_count in people_rows:
            aliases = person.aliases_json if isinstance(person.aliases_json, list) else []
            sample_rows = session.execute(
                select(Face, MediaFile, DerivedAsset)
                .join(MediaFile, MediaFile.file_id == Face.file_id)
                .outerjoin(
                    DerivedAsset,
                    (DerivedAsset.file_id == Face.file_id) & (DerivedAsset.asset_kind == "thumb"),
                )
                .where(Face.person_id == person.id, MediaFile.media_kind == "image", active_media_status)
                .order_by(Face.id.asc(), DerivedAsset.id.asc())
                .limit(4)
            ).all()
            sample_file_ids = []
            sample_faces = []
            seen_sample_files: set[str] = set()
            seen_sample_faces: set[int] = set()
            for face, media_file, asset in sample_rows:
                if face.id in seen_sample_faces:
                    continue
                seen_sample_faces.add(int(face.id))
                if media_file.file_id not in seen_sample_files:
                    sample_file_ids.append(str(media_file.file_id))
                    seen_sample_files.add(str(media_file.file_id))
                sample_faces.append(
                    {
                        "face_id": int(face.id),
                        "file_id": str(media_file.file_id),
                        "filename": media_file.filename,
                        "asset_id": int(asset.id) if asset is not None else None,
                    }
                )
            people_payload.append(
                {
                    "id": person.id,
                    "display_name": person.display_name,
                    "aliases": [str(alias) for alias in aliases if str(alias).strip() and not _INTERNAL_PERSON_ID_RE.match(str(alias).strip())],
                    "face_count": int(face_count or 0),
                    "media_count": int(media_count or 0),
                    "sample_file_ids": sample_file_ids,
                    "sample_faces": sample_faces,
                }
            )
        search_ready_percent = round((search_documents_current / eligible_media_count) * 100, 1) if eligible_media_count else 0
        clip_coverage_percent = round((clip_embeddings_current / eligible_media_count) * 100, 1) if eligible_media_count else 0
        catalog_breakdown = _catalog_breakdown(pipeline_snapshot["media"].get("status_counts") or {})
        resource_settings = resource_settings_snapshot(settings, pipeline)
        ai_summary = _ai_summary(
            eligible_media=eligible_media_count,
            clip_embeddings=clip_embeddings_current,
            completed_images=completed_images,
            completed_videos=completed_videos,
            total_images=total_images,
            semantic_job_errors=semantic_job_errors,
        )
        known_source_roots = [
            str(row[0])
            for row in session.execute(
                select(MediaFile.source_root)
                .where(MediaFile.source_root.is_not(None))
                .distinct()
                .order_by(MediaFile.source_root.asc())
                .limit(20)
            ).all()
            if row[0]
        ]

        return {
            "app": {
                "name": settings.app_name,
                "version": settings.app_version,
            },
            "storage": {
                "data_root": str(settings.data_root),
                "derived_root": str(settings.derived_root),
                "source_roots": [str(path) for path in settings.source_roots],
                "known_source_roots": known_source_roots,
                "database_url": settings.database_url,
                "nas_status": scheduler.nas_status(),
                "nas_last_ping_at": scheduler._last_nas_ping_at.isoformat() if scheduler._last_nas_ping_at else None,
            },
            "security": _security_snapshot(settings),
            "performance": {
                "total_media": total_images,
                "eligible_media": eligible_media_count,
                "search_ready": search_documents_current,
                "search_ready_percent": search_ready_percent,
                "generated_tags": total_tags,
                "tagged_media": tagged_media_count,
                "place_tagged_media": place_tagged_media_count,
                "faces_detected": faces_detected,
                "person_count": person_count,
                "people_media": people_media_count,
                "clip_embeddings": clip_embeddings_current,
                "clip_coverage_percent": clip_coverage_percent,
                "remaining_clip": max(0, eligible_media_count - clip_embeddings_current),
                "ai_summary": ai_summary,
                "resource_settings": resource_settings,
            },
            "catalog": {
                **pipeline_snapshot["media"],
                "breakdown": catalog_breakdown,
            },
            "people": people_payload,
            "people_stats": {
                "total": people_total_count,
                "named": people_named_count,
                "unnamed": max(0, people_total_count - people_named_count),
                "loaded": len(people_payload),
                "load_limit": people_loaded_limit,
            },
            "jobs": {
                **pipeline_snapshot["jobs"],
                "recent": [
                    {
                        "id": job.id,
                        "job_kind": job.job_kind,
                        "status": job.status,
                        "payload_json": job.payload_json,
                        "result_json": job.result_json,
                        "error_stage": job.error_stage,
                        "error_message": job.error_message,
                        "attempts": job.attempts,
                        "enqueued_at": job.enqueued_at,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                        "updated_at": job.updated_at,
                    }
                    for job in recent_jobs
                ],
            },
            "scheduler": serialize_scheduler_snapshot(scheduler.snapshot()),
            "semantic": {
                "scheduler_enabled": settings.semantic_scheduler_enabled,
                "scheduler_interval_seconds": settings.semantic_scheduler_interval_seconds,
                "versions": {
                    "place": settings.semantic_place_version,
                    "person": settings.semantic_person_version,
                    "ocr": settings.semantic_ocr_version,
                    "caption": settings.semantic_caption_version,
                    "embedding": settings.semantic_embedding_version,
                    "auto_tags": settings.semantic_auto_tag_version,
                    "search": settings.semantic_search_version,
                },
                "runtime": {
                    "face_analysis_enabled": settings.face_analysis_enabled,
                    "geocoding_enabled": settings.geocoding_enabled,
                    "place_tag_precision": settings.place_tag_precision,
                },
                "search_documents": {
                    "total": int(session.scalar(select(func.count()).select_from(SearchDocument)) or 0),
                    "version": settings.semantic_search_version,
                },
                "coverage": {
                    "eligible_media": eligible_media_count,
                    "clip_embeddings_current": clip_embeddings_current,
                    "auto_tag_states_current": auto_tag_states_current,
                    "search_current": search_documents_current,
                    "remaining_for_clip": max(0, eligible_media_count - clip_embeddings_current),
                    "remaining_for_auto_tags": max(0, eligible_media_count - auto_tag_states_current),
                    "remaining_for_search": max(0, eligible_media_count - search_documents_current),
                    "remaining_for_face_reanalysis": face_reanalysis_pending,
                    "semantic_job_errors": semantic_job_errors,
                    "clip_model": clip_model_identifier,
                },
            },
            "health": {
                "database_configured": database.configured,
                "waiting_stable": catalog.count_observations(status="waiting_stable"),
                "error": catalog.count_media(status="error"),
                "missing": catalog.count_media(status="missing"),
            },
        }


@router.get("/status/detail/{category}")
async def status_detail(category: str, request: Request) -> dict[str, Any]:
    """Return tag/person breakdown for metric card popups."""
    if category not in {"files", "tags", "places", "people", "ai"}:
        raise HTTPException(status_code=404, detail="unknown category")
    payload = await status(request)
    catalog = payload.get("catalog") or {}
    performance = payload.get("performance") or {}
    database = require_state(request, "database")
    active_status = MediaFile.status.not_in(("missing", "replaced", "excluded"))

    summary: list[dict[str, Any]] = []
    notes: list[str] = []

    if category == "files":
        breakdown = catalog.get("breakdown") or {}
        summary = [
            {"label": "1. 토탈", "count": breakdown.get("total", 0), "sublabel": "현재 처리 대상 사진만 집계"},
            {"label": "2. 완료", "count": breakdown.get("completed", 0), "sublabel": "thumb_done + analysis_done"},
            {"label": "3. 예정", "count": breakdown.get("scheduled", 0), "sublabel": "metadata_done 등 다음 처리 대기"},
            {"label": "4. 미해당", "count": breakdown.get("not_applicable", 0), "sublabel": "missing + replaced + excluded"},
            {"label": "5. 오류", "count": breakdown.get("error", 0), "sublabel": "error 상태"},
        ]
        notes = [str(note) for note in breakdown.get("notes") or []]
        return {"category": category, "summary": summary, "notes": notes, "items": []}

    with database.session_factory() as session:
        items: list[dict] = []

        if category == "tags":
            _AUTO_SECTION = {
                "auto_object": "사물", "auto_scene": "장면",
                "auto_person": "인물 유형", "auto_screen": "화면",
            }
            rows = session.execute(
                select(Tag.tag_type, Tag.tag_value, func.count().label("cnt"))
                .join(MediaFile, MediaFile.file_id == Tag.file_id)
                .where(active_status, MediaFile.media_kind == "image",
                       Tag.tag_type.like("auto_%"))
                .group_by(Tag.tag_type, Tag.tag_value)
                .order_by(func.count().desc())
                .limit(500)
            ).all()
            current_section = None
            for tag_type, tag_value, cnt in rows:
                section = _AUTO_SECTION.get(tag_type, tag_type)
                items.append({"label": tag_value, "sublabel": section if section != current_section else None,
                               "section": section, "count": cnt})
                current_section = section

        elif category == "places":
            _COORD_RE = __import__("re").compile(r"^-?\d+\.\d+,-?\d+\.\d+$")
            rows = session.execute(
                select(Tag.tag_value, func.count(func.distinct(Tag.file_id)).label("cnt"))
                .join(MediaFile, MediaFile.file_id == Tag.file_id)
                .where(active_status, MediaFile.media_kind == "image",
                       Tag.tag_type.in_(("place", "place_detail", "location", "geo_detail")))
                .group_by(Tag.tag_value)
                .order_by(func.count(func.distinct(Tag.file_id)).desc())
                .limit(400)
            ).all()
            for tag_value, cnt in rows:
                if not _COORD_RE.match(str(tag_value)):
                    items.append({"label": tag_value, "count": cnt})

        elif category == "people":
            rows = session.execute(
                select(
                    Person.display_name,
                    func.count(Face.id).filter(active_status).label("face_cnt"),
                    func.count(func.distinct(Face.file_id)).filter(active_status).label("media_cnt"),
                )
                .outerjoin(Face, Face.person_id == Person.id)
                .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
                .group_by(Person.id)
                .having(func.count(Face.id).filter(active_status) >= 1)
                .order_by(func.count(Face.id).filter(active_status).desc())
                .limit(400)
            ).all()
            for display_name, face_cnt, media_cnt in rows:
                items.append({"label": display_name, "sublabel": f"{media_cnt}개 사진", "count": face_cnt})

        elif category == "ai":
            ai_summary = performance.get("ai_summary") or {}
            summary = [
                {"label": "1. 대상", "count": ai_summary.get("eligible_media", 0), "sublabel": "CLIP 검색/자동분류 대상 사진"},
                {"label": "2. 완료", "count": ai_summary.get("clip_embeddings", 0), "sublabel": "CLIP 임베딩 생성 완료"},
                {"label": "3. 예정", "count": ai_summary.get("remaining_clip", 0), "sublabel": "아직 임베딩이 없는 대상 사진"},
                {"label": "4. 미해당", "count": ai_summary.get("not_applicable", 0), "sublabel": "파일 미완료/원본 없음/제외"},
                {"label": "5. 오류", "count": ai_summary.get("error", 0), "sublabel": "이미지 AI 작업 실패 기록"},
            ]
            notes = [str(note) for note in ai_summary.get("notes") or []]
            _AI_SECTION = {
                "auto_object": "사물", "auto_scene": "장면",
                "auto_person": "인물 유형", "auto_screen": "화면",
            }
            rows = session.execute(
                select(Tag.tag_type, Tag.tag_value, func.count().label("cnt"))
                .join(MediaFile, MediaFile.file_id == Tag.file_id)
                .where(active_status, MediaFile.media_kind == "image",
                       Tag.tag_type.in_(list(_AI_SECTION.keys())))
                .group_by(Tag.tag_type, Tag.tag_value)
                .order_by(Tag.tag_type, func.count().desc())
                .limit(500)
            ).all()
            for tag_type, tag_value, cnt in rows:
                section = _AI_SECTION.get(tag_type, tag_type)
                items.append({"label": tag_value, "section": section, "count": cnt})

        return {"category": category, "summary": summary, "notes": notes, "items": items}


@router.post("/scheduler/cycle/{phase}")
async def cycle_scheduler_phase(phase: str, request: Request) -> dict[str, Any]:
    if phase not in {"library", "phase1", "phase2"}:
        raise HTTPException(status_code=404, detail="unknown scheduler phase")
    scheduler = require_state(request, "scheduler")
    snapshot = scheduler.cycle_phase_schedule(phase)
    return {"scheduler": serialize_scheduler_snapshot(snapshot)}
