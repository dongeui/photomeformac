"""AI pack management — model download and readiness endpoints."""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import require_state
from app.services.embedding import clip as clip_embedding

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai-pack", tags=["ai-pack"])

_lock = threading.Lock()
_prepare_thread: threading.Thread | None = None
_prepare_error: str | None = None


def get_ai_pack_state() -> dict[str, Any]:
    """Return current AI pack stage — safe to call from dashboard too."""
    clip_status = clip_embedding.status()
    deps = clip_status.get("dependencies") or {}
    deps_ready = all(deps.get(k) == "installed" for k in ("open_clip_torch", "torch", "torchvision"))

    with _lock:
        downloading = _prepare_thread is not None and _prepare_thread.is_alive()
        error = _prepare_error

    if clip_status.get("model_ready"):
        stage = "ready"
    elif error:
        stage = "error"
    elif downloading or clip_status.get("model_loading"):
        stage = "downloading"
    elif deps_ready:
        stage = "needs_download"
    else:
        stage = "needs_packages"

    return {
        "stage": stage,
        "deps_ready": deps_ready,
        "model_ready": bool(clip_status.get("model_ready")),
        "model_loading": downloading or bool(clip_status.get("model_loading")),
        "model_error": error or clip_status.get("model_error"),
        "dependencies": deps,
        "config": clip_status.get("config") or {},
    }


@router.get("/status")
async def ai_pack_status() -> JSONResponse:
    return JSONResponse(get_ai_pack_state())


@router.post("/prepare")
async def ai_pack_prepare(
    request: Request,
    load_cached: bool = Query(default=False),
) -> JSONResponse:
    global _prepare_thread, _prepare_error

    settings = require_state(request, "settings")
    state = get_ai_pack_state()
    if state["stage"] == "ready":
        return JSONResponse({"ok": True, "message": "Model already ready."})
    if state["stage"] == "downloading":
        return JSONResponse({"ok": True, "message": "Download already in progress."})
    if settings.offline_mode and not load_cached:
        return JSONResponse(
            {
                "ok": False,
                "message": (
                    "Offline mode blocks automatic model downloads. Use load_cached=true to activate an "
                    "existing cache, or prepare the local AI model cache while online and restart in offline mode."
                ),
            },
            status_code=409,
        )
    if state["stage"] == "needs_packages":
        return JSONResponse(
            {"ok": False, "message": "Install photome[clip] packages first."},
            status_code=400,
        )

    with _lock:
        if _prepare_thread is not None and _prepare_thread.is_alive():
            return JSONResponse({"ok": True, "message": "Download already in progress."})
        _prepare_error = None

        def _run() -> None:
            global _prepare_error
            try:
                clip_embedding.ensure_models()
                logger.info("AI pack model prepare complete")
            except Exception as exc:
                logger.error("AI pack prepare failed: %s", exc)
                with _lock:
                    _prepare_error = str(exc)

        _prepare_thread = threading.Thread(target=_run, daemon=True, name="ai-pack-prepare")
        _prepare_thread.start()

    message = "Cached model load started." if settings.offline_mode else "Download started."
    return JSONResponse({"ok": True, "message": message})


@router.get("/progress")
async def ai_pack_progress() -> JSONResponse:
    return JSONResponse(get_ai_pack_state())
