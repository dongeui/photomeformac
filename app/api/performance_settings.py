"""Dashboard-editable performance/resource settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import require_state
from app.core.settings import AppSettings, asset_worker_cap, load_settings, torch_thread_cap
from app.services.embedding import clip as clip_embedding

router = APIRouter(tags=["settings"])


class PerformanceSettingsPayload(BaseModel):
    asset_processing_workers: int = Field(ge=1)
    torch_threads: int = Field(ge=1)
    semantic_maintenance_batch_size: int = Field(ge=50, le=5000)
    semantic_manual_batch_size: int = Field(ge=50, le=5000)


def env_file_path() -> Path:
    configured = os.environ.get("PHOTOME_ENV_FILE", ".env")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resource_settings_snapshot(settings: AppSettings, pipeline: Any) -> dict[str, Any]:
    runtime = {}
    if pipeline is not None and hasattr(pipeline, "status_snapshot"):
        try:
            runtime = (pipeline.status_snapshot().get("runtime") or {})
        except Exception:
            runtime = {}
    cpu_cap = torch_thread_cap()
    worker_cap_value = asset_worker_cap()
    workers = int(runtime.get("asset_processing_workers") or settings.asset_processing_workers)
    torch_threads = int(settings.torch_threads or max(1, cpu_cap // max(1, workers)))
    maintenance_batch = int(runtime.get("semantic_maintenance_batch_size") or settings.semantic_maintenance_batch_size)
    manual_batch = int(runtime.get("semantic_manual_batch_size") or settings.semantic_manual_batch_size)
    return {
        "asset_processing_workers": workers,
        "asset_processing_workers_cap": worker_cap_value,
        "torch_threads": torch_threads,
        "torch_threads_cap": cpu_cap,
        "semantic_maintenance_batch_size": maintenance_batch,
        "semantic_manual_batch_size": manual_batch,
        "cpu_profile_label": _cpu_profile_label(workers, worker_cap_value),
        "memory_profile_label": _memory_profile_label(maintenance_batch, manual_batch),
        "env_file": str(env_file_path()),
    }


@router.post("/settings/performance")
async def update_performance_settings(request: Request, payload: PerformanceSettingsPayload) -> dict[str, Any]:
    pipeline = require_state(request, "pipeline")

    worker_cap_value = asset_worker_cap()
    cpu_cap = torch_thread_cap()
    workers = max(1, min(worker_cap_value, int(payload.asset_processing_workers)))
    torch_threads = max(1, min(cpu_cap, int(payload.torch_threads)))
    maintenance_batch = max(50, min(5000, int(payload.semantic_maintenance_batch_size)))
    manual_batch = max(50, min(5000, int(payload.semantic_manual_batch_size)))

    updates = {
        "PHOTOME_ASSET_PROCESSING_WORKERS": str(workers),
        "PHOTOME_TORCH_THREADS": str(torch_threads),
        "PHOTOME_SEMANTIC_MAINTENANCE_BATCH_SIZE": str(maintenance_batch),
        "PHOTOME_SEMANTIC_MANUAL_BATCH_SIZE": str(manual_batch),
    }
    _update_env_file(env_file_path(), updates)
    for key, value in updates.items():
        os.environ[key] = value

    try:
        clip_embedding._configure_torch_threads()  # type: ignore[attr-defined]
    except Exception:
        pass

    pipeline.update_resource_settings(
        asset_processing_workers=workers,
        semantic_maintenance_batch_size=maintenance_batch,
        semantic_manual_batch_size=manual_batch,
    )

    request.app.state.settings = load_settings()
    active_job = pipeline.has_active_library_job()
    snapshot = resource_settings_snapshot(request.app.state.settings, pipeline)
    return {
        "saved": True,
        "applied_to_next_jobs": True,
        "restart_recommended": True,
        "active_job_running": active_job,
        "message": (
            "저장 완료. 새로 시작하는 동기화/이미지AI 작업부터 반영됩니다. "
            "현재 돌고 있는 작업이 있으면 그 작업은 기존 값으로 끝나고, 앱 재시작까지 하면 CPU 스레드도 완전히 맞춰집니다."
        ),
        "settings": snapshot,
    }


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _sep, _value = line.partition("=")
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1].strip():
        output.append("")
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _cpu_profile_label(workers: int, cap: int) -> str:
    ratio = workers / max(1, cap)
    if ratio >= 0.85:
        return "최대"
    if ratio >= 0.6:
        return "고성능"
    if ratio >= 0.35:
        return "균형"
    return "절약"


def _memory_profile_label(maintenance_batch: int, manual_batch: int) -> str:
    score = max(maintenance_batch, manual_batch)
    if score >= 1500:
        return "높음"
    if score >= 700:
        return "보통"
    return "낮음"
