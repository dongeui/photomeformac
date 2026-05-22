"""Person management API — list, rename, and query face clusters."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update

from app.api.deps import require_state
from app.models.asset import DerivedAsset
from app.models.face import Face
from app.models.media import MediaFile
from app.models.person import Person
from app.models.tag import Tag
from app.services.image_decode import ensure_heif_support
from app.services.semantic import SemanticCatalog
from app.services.search.hybrid import clear_query_cache
from app.services.search.vocab import TagVocabularyCache

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

router = APIRouter(prefix="/people", tags=["people"])


class PersonResponse(BaseModel):
    id: int
    display_name: str
    aliases: list[str]
    face_count: int
    media_count: int
    sample_file_ids: list[str]
    sample_face_ids: list[int]


class PersonPreviewItem(BaseModel):
    file_id: str
    filename: str
    relative_path: str
    media_kind: str
    captured_at: Optional[str]
    asset_id: Optional[int]
    face_id: Optional[int] = None


class PersonPreviewResponse(BaseModel):
    person: PersonResponse
    items: list[PersonPreviewItem]


class RenamePersonRequest(BaseModel):
    display_name: str
    aliases: Optional[list[str]] = None


class MergePeopleRequest(BaseModel):
    target_person_id: int
    source_person_ids: list[int]


class AssignFaceRequest(BaseModel):
    person_id: Optional[int] = None


@router.get("", response_model=list[PersonResponse])
def list_people(request: Request) -> list[PersonResponse]:
    """List all known persons with face counts."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        rows = session.execute(
            select(
                Person,
                func.count(Face.id).filter(_active_media_predicate()).label("face_count"),
                func.count(func.distinct(Face.file_id)).filter(_active_media_predicate()).label("media_count"),
            )
            .outerjoin(Face, Face.person_id == Person.id)
            .outerjoin(MediaFile, MediaFile.file_id == Face.file_id)
            .group_by(Person.id)
            .having(func.count(Face.id).filter(_active_media_predicate()) > 0)
            .order_by(func.count(Face.id).filter(_active_media_predicate()).desc())
        ).all()
        result: list[PersonResponse] = []
        for person, face_count, media_count in rows:
            sample_faces = session.scalars(
                select(Face)
                .join(MediaFile, MediaFile.file_id == Face.file_id)
                .where(Face.person_id == person.id, _active_media_predicate())
                .limit(3)
            ).all()
            result.append(
                PersonResponse(
                    id=person.id,
                    display_name=person.display_name,
                    aliases=_person_display_aliases(person),
                    face_count=face_count,
                    media_count=media_count,
                    sample_file_ids=[str(f.file_id) for f in sample_faces],
                    sample_face_ids=[int(f.id) for f in sample_faces],
                )
            )
        return result


@router.get("/{person_id}", response_model=PersonResponse)
def get_person(person_id: int, request: Request) -> PersonResponse:
    database = require_state(request, "database")
    with database.session_factory() as session:
        person = session.get(Person, person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        face_count = _person_face_count(session, person_id)
        sample_faces = session.scalars(
            select(Face)
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.person_id == person_id, _active_media_predicate())
            .limit(3)
        ).all()
        return PersonResponse(
            id=person.id,
            display_name=person.display_name,
            aliases=_person_display_aliases(person),
            face_count=face_count,
            media_count=_person_media_count(session, person_id),
            sample_file_ids=[str(f.file_id) for f in sample_faces],
            sample_face_ids=[int(f.id) for f in sample_faces],
        )


@router.post("/merge", response_model=PersonResponse)
def merge_people(body: MergePeopleRequest, request: Request) -> PersonResponse:
    """Merge multiple face clusters into one user-selected person."""
    source_ids = _normalize_merge_source_ids(body.source_person_ids, body.target_person_id)
    if not source_ids:
        raise HTTPException(status_code=422, detail="select at least one source person")

    database = require_state(request, "database")
    search_version = request.app.state.settings.semantic_search_version
    with database.session_factory() as session:
        target = session.get(Person, body.target_person_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Target person not found")
        sources = session.scalars(select(Person).where(Person.id.in_(source_ids))).all()
        found_source_ids = {int(person.id) for person in sources}
        missing_ids = [person_id for person_id in source_ids if person_id not in found_source_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail=f"Source person not found: {missing_ids[0]}")

        old_labels = set(_person_labels(target))
        for source in sources:
            old_labels.update(_person_labels(source))

        merged_aliases = _merge_person_aliases(target, sources)
        session.execute(update(Face).where(Face.person_id.in_(source_ids)).values(person_id=target.id))
        target.aliases_json = merged_aliases
        for source in sources:
            session.delete(source)

        _sync_person_search_labels(session, target, old_labels=old_labels, search_version=search_version)
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        session.refresh(target)
        return _person_response(session, target)


@router.patch("/{person_id}", response_model=PersonResponse)
def rename_person(
    person_id: int,
    body: RenamePersonRequest,
    request: Request,
) -> PersonResponse:
    """Update the display name for a person (face cluster)."""
    database = require_state(request, "database")
    new_name = body.display_name.strip()
    if not new_name:
        raise HTTPException(status_code=422, detail="display_name must not be empty")
    if _INTERNAL_PERSON_ID_RE.match(new_name):
        raise HTTPException(status_code=422, detail="display_name cannot be an internal cluster ID")
    with database.session_factory() as session:
        person = session.get(Person, person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        old_labels = _person_labels(person)
        person.display_name = new_name
        person.aliases_json = _normalize_aliases(_validate_user_aliases(body.aliases or []))
        _sync_person_search_labels(
            session,
            person,
            old_labels=old_labels,
            search_version=request.app.state.settings.semantic_search_version,
        )
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
        session.refresh(person)
        return _person_response(session, person)


@router.get("/{person_id}/media", response_model=list[str])
def list_person_media(
    person_id: int,
    request: Request,
    limit: int = 50,
) -> list[str]:
    """Return file_ids of media containing this person."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        person = session.get(Person, person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        file_ids = session.scalars(
            select(Face.file_id)
            .where(Face.person_id == person_id)
            .distinct()
            .limit(limit)
        ).all()
        return [str(fid) for fid in file_ids]


@router.get("/{person_id}/preview", response_model=PersonPreviewResponse)
def preview_person_media(
    person_id: int,
    request: Request,
    limit: int = 48,
) -> PersonPreviewResponse:
    """Return lightweight media cards for the dashboard person preview modal."""
    database = require_state(request, "database")
    bounded_limit = min(max(int(limit), 1), 96)
    with database.session_factory() as session:
        person = session.get(Person, person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        rows = session.execute(
            select(MediaFile, DerivedAsset, func.min(Face.id).label("face_id"))
            .join(Face, Face.file_id == MediaFile.file_id)
            .outerjoin(
                DerivedAsset,
                (DerivedAsset.file_id == MediaFile.file_id) & (DerivedAsset.asset_kind == "thumb"),
            )
            .where(Face.person_id == person_id, _active_media_predicate())
            .group_by(MediaFile.file_id)
            .order_by(_captured_at_expr().desc(), MediaFile.file_id.desc())
            .limit(bounded_limit)
        ).all()
        items = [
            PersonPreviewItem(
                file_id=str(media_file.file_id),
                filename=media_file.filename,
                relative_path=media_file.relative_path,
                media_kind=media_file.media_kind,
                captured_at=media_file.exif_datetime.isoformat() if media_file.exif_datetime else None,
                asset_id=int(asset.id) if asset is not None else None,
                face_id=int(face_id) if face_id is not None else None,
            )
            for media_file, asset, face_id in rows
        ]
        return PersonPreviewResponse(person=_person_response(session, person), items=items)


@router.patch("/faces/{face_id}", status_code=204)
def assign_face(face_id: int, body: AssignFaceRequest, request: Request) -> Response:
    """Reassign or unassign a single face to a different person."""
    database = require_state(request, "database")
    search_version = request.app.state.settings.semantic_search_version
    with database.session_factory() as session:
        face = session.get(Face, face_id)
        if face is None:
            raise HTTPException(status_code=404, detail="Face not found")
        if body.person_id is not None and session.get(Person, body.person_id) is None:
            raise HTTPException(status_code=404, detail="Target person not found")
        file_id = str(face.file_id)
        face.person_id = body.person_id
        _sync_single_file_person_labels(session, file_id, search_version=search_version)
        session.commit()
        clear_query_cache()
        TagVocabularyCache.invalidate()
    return Response(status_code=204)


@router.get("/faces/{face_id}/crop")
def face_crop(face_id: int, request: Request) -> Response:
    """Return a small local-only face crop for person-name assignment UI."""
    if Image is None:
        raise HTTPException(status_code=503, detail="pillow is required for face crop rendering")
    database = require_state(request, "database")
    settings = require_state(request, "settings")
    with database.session_factory() as session:
        row = session.execute(
            select(Face, MediaFile)
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.id == face_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="face not found")
        face, media_file = row
        asset = session.scalars(
            select(DerivedAsset)
            .where(DerivedAsset.file_id == media_file.file_id, DerivedAsset.asset_kind == "thumb")
            .order_by(DerivedAsset.id.asc())
            .limit(1)
        ).first()

    source_path = Path(media_file.current_path)
    scale_from_original = False
    if not source_path.is_file():
        if asset is None:
            raise HTTPException(status_code=404, detail="face source image missing")
        source_path = _resolve_derived_path(settings.derived_root, asset.derived_path)
        scale_from_original = True
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail="face source image missing")

    try:
        ensure_heif_support()
        with Image.open(source_path) as image:
            image = image.convert("RGB")
            bbox = _scaled_face_bbox(
                face.bbox or {},
                image_size=image.size,
                original_size=(media_file.width, media_file.height),
                scale_from_original=scale_from_original,
            )
            cropped = image.crop(bbox)
            cropped.thumbnail((180, 180))
            output = BytesIO()
            cropped.save(output, format="JPEG", quality=88, optimize=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"face crop failed: {exc}") from exc

    return Response(
        output.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


import re as _re
_INTERNAL_PERSON_ID_RE = _re.compile(r"^person-\d+$", _re.IGNORECASE)


def _person_aliases(person: Person) -> list[str]:
    """All aliases including internal person-XXXXXXXX IDs (used for search tag expansion)."""
    raw = person.aliases_json or []
    if not isinstance(raw, list):
        return []
    return _normalize_aliases([str(value) for value in raw])


def _person_display_aliases(person: Person) -> list[str]:
    """Human-readable aliases only — internal person-XXXXXXXX IDs are excluded from UI."""
    return [a for a in _person_aliases(person) if not _INTERNAL_PERSON_ID_RE.match(a)]


def _person_labels(person: Person) -> set[str]:
    labels = {person.display_name.strip()}
    labels.update(_person_aliases(person))
    return {label for label in labels if label}


def _normalize_aliases(values: list[str]) -> list[str]:
    seen: set[str] = set()
    aliases: list[str] = []
    for value in values:
        alias = " ".join(str(value).strip().split())
        folded = alias.casefold()
        if not alias or folded in seen:
            continue
        seen.add(folded)
        aliases.append(alias[:128])
    return aliases[:20]


def _validate_user_aliases(values: list[str]) -> list[str]:
    """Filter out internal cluster IDs from user-supplied alias lists."""
    return [a for a in values if not _INTERNAL_PERSON_ID_RE.match(a.strip())]


def _person_media_count(session, person_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(Face.file_id)))
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.person_id == person_id, _active_media_predicate())
        )
        or 0
    )


def _person_face_count(session, person_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Face)
            .join(MediaFile, MediaFile.file_id == Face.file_id)
            .where(Face.person_id == person_id, _active_media_predicate())
        )
        or 0
    )


def _person_response(session, person: Person) -> PersonResponse:
    sample_faces = session.scalars(
        select(Face)
        .join(MediaFile, MediaFile.file_id == Face.file_id)
        .where(Face.person_id == person.id, _active_media_predicate())
        .limit(3)
    ).all()
    return PersonResponse(
        id=person.id,
        display_name=person.display_name,
        aliases=_person_display_aliases(person),
        face_count=_person_face_count(session, person.id),
        media_count=_person_media_count(session, person.id),
        sample_file_ids=[str(f.file_id) for f in sample_faces],
        sample_face_ids=[int(f.id) for f in sample_faces],
    )


def _active_media_predicate():
    return MediaFile.status.not_in(("missing", "replaced", "excluded"))


def _captured_at_expr():
    mtime_expr = func.datetime(MediaFile.mtime_ns / 1000000000, "unixepoch", "localtime")
    return func.coalesce(MediaFile.exif_datetime, mtime_expr, MediaFile.processed_at, MediaFile.last_seen_at)


def _normalize_merge_source_ids(source_ids: list[int], target_person_id: int) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for source_id in source_ids:
        person_id = int(source_id)
        if person_id == int(target_person_id) or person_id in seen:
            continue
        seen.add(person_id)
        normalized.append(person_id)
    return normalized


def _merge_person_aliases(target: Person, sources: list[Person]) -> list[str]:
    aliases = list(_person_aliases(target))
    for source in sources:
        aliases.append(source.display_name)
        aliases.extend(_person_aliases(source))
    return [alias for alias in _normalize_aliases(aliases) if alias.casefold() != target.display_name.casefold()]


def _sync_single_file_person_labels(session, file_id: str, *, search_version: str) -> None:
    """Re-sync all person tags for a single file after a face reassignment."""
    persons_in_file = session.scalars(
        select(Person)
        .join(Face, Face.person_id == Person.id)
        .where(Face.file_id == file_id)
        .distinct()
    ).all()
    session.execute(
        delete(Tag).where(Tag.file_id == file_id, Tag.tag_type == "person")
    )
    for person in persons_in_file:
        for label in _person_labels(person):
            session.add(Tag(file_id=file_id, tag_type="person", tag_value=label))
    media_file = session.get(MediaFile, file_id)
    if media_file:
        SemanticCatalog(session).upsert_search_document(media_file, version=search_version)


def _sync_person_search_labels(session, person: Person, *, old_labels: set[str], search_version: str) -> None:
    labels = _person_labels(person)
    affected_file_ids = [
        str(file_id)
        for file_id in session.scalars(
            select(Face.file_id).where(Face.person_id == person.id).distinct()
        )
    ]
    if not affected_file_ids:
        return

    removable = {label.casefold() for label in (old_labels | labels) if label}
    if removable:
        session.execute(
            delete(Tag).where(
                Tag.file_id.in_(affected_file_ids),
                Tag.tag_type == "person",
                func.lower(Tag.tag_value).in_(list(removable)),
            )
        )

    for file_id in affected_file_ids:
        for label in labels:
            session.add(Tag(file_id=file_id, tag_type="person", tag_value=label))

    semantic = SemanticCatalog(session)
    media_files = session.scalars(select(MediaFile).where(MediaFile.file_id.in_(affected_file_ids))).all()
    for media_file in media_files:
        semantic.upsert_search_document(media_file, version=search_version)


def _resolve_derived_path(derived_root: Path, derived_path: str) -> Path:
    candidate = Path(derived_path)
    if candidate.is_absolute():
        return candidate
    root = derived_root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="face source image missing") from exc
    return resolved


def _scaled_face_bbox(
    bbox: dict,
    *,
    image_size: tuple[int, int],
    original_size: tuple[Optional[int], Optional[int]],
    scale_from_original: bool,
) -> tuple[int, int, int, int]:
    image_width, image_height = image_size
    x = float(bbox.get("x") or 0)
    y = float(bbox.get("y") or 0)
    width = max(1.0, float(bbox.get("width") or image_width))
    height = max(1.0, float(bbox.get("height") or image_height))
    if scale_from_original and original_size[0] and original_size[1]:
        x *= image_width / float(original_size[0])
        width *= image_width / float(original_size[0])
        y *= image_height / float(original_size[1])
        height *= image_height / float(original_size[1])
    pad = max(width, height) * 0.38
    left = max(0, int(round(x - pad)))
    top = max(0, int(round(y - pad)))
    right = min(image_width, int(round(x + width + pad)))
    bottom = min(image_height, int(round(y + height + pad)))
    if right <= left or bottom <= top:
        return (0, 0, image_width, image_height)
    return (left, top, right, bottom)
