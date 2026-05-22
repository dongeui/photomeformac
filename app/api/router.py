"""Application API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.ai_pack import router as ai_pack_router
from app.api.gallery import router as gallery_router
from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.people import router as people_router
from app.api.scan import router as scan_router
from app.api.search import router as search_router
from app.api.status import router as status_router


api_router = APIRouter()
api_router.include_router(ai_pack_router)
api_router.include_router(gallery_router)
api_router.include_router(health_router)
api_router.include_router(media_router)
api_router.include_router(people_router)
api_router.include_router(scan_router)
api_router.include_router(search_router)
api_router.include_router(status_router)
