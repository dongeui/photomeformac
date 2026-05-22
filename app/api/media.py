"""Media listing and detail endpoints."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Optional

from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete

from app.api.deps import require_state
from app.api.serializers import serialize_media_file
from app.models.asset import DerivedAsset
from app.models.annotation import MediaAnnotation
from app.models.tag import Tag
from app.services.processing.registry import MediaCatalog
from app.services.semantic import SemanticCatalog


router = APIRouter(prefix="/media", tags=["media"])


@router.get("/filter")
async def filter_media(
    request: Request,
    status: Optional[str] = Query(default=None),
    media_kind: Optional[str] = Query(default=None),
    source_root: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    tag_type: Optional[str] = Query(default=None),
    face_count_min: Optional[int] = Query(default=None, ge=0),
    face_count_max: Optional[int] = Query(default=None, ge=0),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return await _list_media(
        request,
        status,
        media_kind,
        source_root,
        date_from,
        date_to,
        tag,
        tag_type,
        face_count_min,
        face_count_max,
        q,
        limit,
        offset,
    )


@router.get("")
async def list_media(
    request: Request,
    status: Optional[str] = Query(default=None),
    media_kind: Optional[str] = Query(default=None),
    source_root: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    tag_type: Optional[str] = Query(default=None),
    face_count_min: Optional[int] = Query(default=None, ge=0),
    face_count_max: Optional[int] = Query(default=None, ge=0),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return await _list_media(
        request,
        status,
        media_kind,
        source_root,
        date_from,
        date_to,
        tag,
        tag_type,
        face_count_min,
        face_count_max,
        q,
        limit,
        offset,
    )


@router.get("/{file_id}")
async def get_media(request: Request, file_id: str) -> dict[str, Any]:
    database = require_state(request, "database")
    with database.session_factory() as session:
        catalog = MediaCatalog(session)
        media_file = catalog.get_media(file_id)
        if media_file is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media file not found")

        assets = session.query(DerivedAsset).filter(DerivedAsset.file_id == file_id).order_by(DerivedAsset.asset_kind).all()
        return {
            "item": serialize_media_file(media_file),
            "annotation": _serialize_annotation(session.get(MediaAnnotation, file_id)),
            "tags": [
                {
                    "id": tag.id,
                    "tag_type": tag.tag_type,
                    "tag_value": tag.tag_value,
                }
                for tag in media_file.tags
            ],
            "faces": [
                {
                    "id": face.id,
                    "person_id": face.person_id,
                    "bbox": face.bbox,
                    "embedding_ref": face.embedding_ref,
                }
                for face in media_file.faces
            ],
            "derived_assets": [
                {
                    "id": asset.id,
                    "file_id": asset.file_id,
                    "asset_kind": asset.asset_kind,
                    "asset_version": asset.asset_version,
                    "derived_path": asset.derived_path,
                    "content_type": asset.content_type,
                    "checksum": asset.checksum,
                    "created_at": asset.created_at,
                    "updated_at": asset.updated_at,
                }
                for asset in assets
            ],
        }


@router.post("/{file_id}/annotation")
async def update_media_annotation(request: Request, file_id: str):
    database = require_state(request, "database")
    raw_body = (await request.body()).decode("utf-8")
    form = parse_qs(raw_body, keep_blank_values=True)
    title = _form_value(form, "title")
    description = _form_value(form, "description")
    custom_tags = _parse_custom_tags(_form_value(form, "tags"))
    next_url = _safe_next_url(_form_value(form, "next") or request.headers.get("referer"), file_id)

    with database.session_factory() as session:
        catalog = MediaCatalog(session)
        media_file = catalog.get_media(file_id)
        if media_file is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media file not found")

        annotation = session.get(MediaAnnotation, file_id)
        if annotation is None and (title or description):
            annotation = MediaAnnotation(file_id=file_id)
            session.add(annotation)
        if annotation is not None:
            annotation.title = title or None
            annotation.description = description or None

        session.execute(delete(Tag).where(Tag.file_id == file_id, Tag.tag_type == "custom"))
        for tag_value in custom_tags:
            session.add(Tag(file_id=file_id, tag_type="custom", tag_value=tag_value))
        media_file.updated_at = datetime.utcnow()
        SemanticCatalog(session).upsert_search_document(
            media_file,
            version=request.app.state.settings.semantic_search_version,
        )
        session.commit()

    return RedirectResponse(next_url, status_code=303)


async def _list_media(
    request: Request,
    status: Optional[str],
    media_kind: Optional[str],
    source_root: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    tag: Optional[str],
    tag_type: Optional[str],
    face_count_min: Optional[int],
    face_count_max: Optional[int],
    q: Optional[str],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    database = require_state(request, "database")
    date_from_dt = _start_of_day(date_from)
    date_to_dt = _end_of_day(date_to)
    with database.session_factory() as session:
        catalog = MediaCatalog(session)
        items = catalog.list_media(
            limit=limit,
            offset=offset,
            status=status,
            media_kind=media_kind,
            source_root=source_root,
            query=q,
            date_from=date_from_dt,
            date_to=date_to_dt,
            tag=tag,
            tag_type=tag_type,
            face_count_min=face_count_min,
            face_count_max=face_count_max,
        )
        total = catalog.count_media(
            status=status,
            media_kind=media_kind,
            source_root=source_root,
            query=q,
            date_from=date_from_dt,
            date_to=date_to_dt,
            tag=tag,
            tag_type=tag_type,
            face_count_min=face_count_min,
            face_count_max=face_count_max,
        )
        return {
            "items": [serialize_media_file(item) for item in items],
            "total": total,
            "limit": limit,
            "offset": offset,
            "filters": {
                "status": status,
                "media_kind": media_kind,
                "source_root": source_root,
                "date_from": date_from,
                "date_to": date_to,
                "tag": tag,
                "tag_type": tag_type,
                "face_count_min": face_count_min,
                "face_count_max": face_count_max,
                "q": q,
            },
        }


def _start_of_day(value: Optional[date]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.combine(value, time.min)


def _end_of_day(value: Optional[date]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.combine(value, time.max)


def _serialize_annotation(annotation: MediaAnnotation | None) -> dict[str, str | None]:
    if annotation is None:
        return {"title": None, "description": None}
    return {"title": annotation.title, "description": annotation.description}


def _form_value(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key) or [""]
    return values[0].strip()


def _parse_custom_tags(value: str) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for raw_tag in value.replace("#", ",").replace(";", ",").split(","):
        tag = raw_tag.strip()
        normalized = tag.casefold()
        if not tag or normalized in seen:
            continue
        seen.add(normalized)
        tags.append(tag[:256])
    return tags


def _safe_next_url(value: str | None, file_id: str) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return f"/gallery#card-{file_id}"
