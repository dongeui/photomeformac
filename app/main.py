"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.router import api_router
from app.core.lifecycle import lifespan
from app.core.settings import AppSettings, load_settings


logger = logging.getLogger(__name__)


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = None
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
