"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.lifecycle import lifespan
from app.core.settings import AppSettings, load_settings


logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROTECTED_LAN_PREFIXES = (
    "/scan",
    "/people",
    "/search/weights",
    "/search/feedback",
    "/search/index",
    "/search/debug",
    "/search/benchmark",
)
_PROTECTED_LAN_PARTS = ("/download", "/original", "/admin")


def _lan_admin_required(request: Request, settings: AppSettings) -> bool:
    if settings.server_host not in {"0.0.0.0", "::"}:
        return False
    client_host = request.client.host if request.client else ""
    if client_host in _LOOPBACK_HOSTS:
        return False
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        return True
    path = request.url.path
    return path.startswith(_PROTECTED_LAN_PREFIXES) or any(part in path for part in _PROTECTED_LAN_PARTS)


def _lan_admin_authorized(request: Request, settings: AppSettings) -> bool:
    token = settings.lan_admin_token.strip()
    if not token:
        return False
    provided = request.headers.get("x-photome-admin-token") or request.query_params.get("admin_token") or ""
    return provided == token


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = None

    @app.middleware("http")
    async def lan_admin_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if _lan_admin_required(request, resolved_settings) and not _lan_admin_authorized(request, resolved_settings):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "LAN 공유 보호가 켜져 있습니다. 관리자 작업은 X-Photome-Admin-Token이 필요합니다."
                },
            )
        return await call_next(request)

    app.include_router(api_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings: AppSettings = app.state.settings
    from app.core.logging import configure_logging

    configure_logging(settings.log_level)
    logger.info(
        "starting server",
        extra={
            "host": settings.server_host,
            "port": settings.server_port,
            "reload": settings.reload,
        },
    )
    if settings.reload:
        uvicorn.run(
            "app.main:app",
            host=settings.server_host,
            port=settings.server_port,
            reload=True,
            log_level=settings.log_level.lower(),
        )
        return

    uvicorn.run(
        app,
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level.lower(),
    )
