"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request


router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, object]:
    settings = getattr(request.app.state, "settings", None)
    database = getattr(request.app.state, "database", None)
    return {
        "status": "ready",
        "app_name": settings.app_name if settings is not None else "photome",
        "app_version": settings.app_version if settings is not None else "0.1.0",
        "database_configured": bool(database and database.configured),
    }
