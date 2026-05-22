"""Search endpoints."""

from __future__ import annotations

from datetime import date, datetime, time
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import require_state
from app.api.status import _security_snapshot
from app.models.semantic import SearchDocument, SearchFeedback, SearchWeightProfile
from app.services.search import HybridSearchService
from app.services.search.backend import SqlAlchemyHybridSearchBackend
from app.services.search.benchmark import run_benchmark_suite
from app.services.search.hybrid import clear_query_cache, intent_weights
from app.services.search.vector import invalidate_global_vector_index


router = APIRouter(tags=["search"])
logger = logging.getLogger(__name__)


@router.get("/search")
async def search_media(
    request: Request,
    q: str = Query(default=""),
    mode: str = Query(default="hybrid"),
    place: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    limit: int = Query(default=40, ge=1, le=100),
    debug: bool = Query(default=False),
    w_ocr: Optional[float] = Query(default=None, ge=0),
    w_clip: Optional[float] = Query(default=None, ge=0),
    w_shadow: Optional[float] = Query(default=None, ge=0),
) -> dict[str, Any]:
    return _search_payload(
        request,
        q=q,
        mode=mode,
        place=place,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        debug=debug,
        weight_overrides=_weight_overrides(w_ocr=w_ocr, w_clip=w_clip, w_shadow=w_shadow),
    )


@router.get("/search/debug")
async def search_media_debug(
    request: Request,
    q: str = Query(default=""),
    mode: str = Query(default="hybrid"),
    place: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    w_ocr: Optional[float] = Query(default=None, ge=0),
    w_clip: Optional[float] = Query(default=None, ge=0),
    w_shadow: Optional[float] = Query(default=None, ge=0),
) -> dict[str, Any]:
    return _search_payload(
        request,
        q=q,
        mode=mode,
        place=place,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        debug=True,
        weight_overrides=_weight_overrides(w_ocr=w_ocr, w_clip=w_clip, w_shadow=w_shadow),
    )


@router.get("/search/benchmark")
async def search_benchmark(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    w_ocr: Optional[float] = Query(default=None, ge=0),
    w_clip: Optional[float] = Query(default=None, ge=0),
    w_shadow: Optional[float] = Query(default=None, ge=0),
) -> dict[str, Any]:
    database = require_state(request, "database")
    settings = require_state(request, "settings")
    with database.session_factory() as session:
        backend = SqlAlchemyHybridSearchBackend(
            session,
            embeddings_root=settings.embeddings_root,
            clip_enabled=settings.semantic_clip_enabled,
            log_events=False,
        )
        service = HybridSearchService(backend)
        return run_benchmark_suite(
            service,
            limit=limit,
            weight_overrides=_weight_overrides(w_ocr=w_ocr, w_clip=w_clip, w_shadow=w_shadow),
        )


def _search_payload(
    request: Request,
    *,
    q: str,
    mode: str,
    place: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = 40,
    debug: bool = False,
    weight_overrides: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    if not q.strip():
        return {
            "items": [],
            "total": 0,
            "query": q,
            "security": _security_snapshot(require_state(request, "settings")),
            "meta": {
                "effective_mode": mode,
                "intent_reason": "empty",
                "weight_overrides": weight_overrides or {},
            },
        }

    database = require_state(request, "database")
    settings = require_state(request, "settings")
    pipeline = require_state(request, "pipeline")
    log_events = not pipeline.has_active_library_job()
    with database.session_factory() as session:
        backend = SqlAlchemyHybridSearchBackend(
            session,
            embeddings_root=settings.embeddings_root,
            clip_enabled=settings.semantic_clip_enabled,
            log_events=log_events,
        )
        service = HybridSearchService(backend)
        items, meta = service.search_with_meta(
            q,
            limit=limit,
            place_filter=place,
            date_from=_start_of_day(date_from),
            date_to=_end_of_day(date_to),
            mode=mode,
            debug=debug,
            weight_overrides=weight_overrides,
        )
        try:
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            logger.warning("search event persistence skipped", extra={"error": str(exc), "query": q})
    return {
        "items": items,
        "total": len(items),
        "query": q,
        "security": _security_snapshot(settings),
        "meta": meta,
    }


def _start_of_day(value: Optional[date]) -> Optional[datetime]:
    return None if value is None else datetime.combine(value, time.min)


def _end_of_day(value: Optional[date]) -> Optional[datetime]:
    return None if value is None else datetime.combine(value, time.max)


def _weight_overrides(
    *,
    w_ocr: Optional[float],
    w_clip: Optional[float],
    w_shadow: Optional[float],
) -> dict[str, float]:
    overrides: dict[str, float] = {}
    if w_ocr is not None:
        overrides["ocr"] = w_ocr
    if w_clip is not None:
        overrides["clip"] = w_clip
    if w_shadow is not None:
        overrides["shadow"] = w_shadow
    return overrides


# ---------------------------------------------------------------------------
# Weight profile API
# ---------------------------------------------------------------------------

class WeightProfileResponse(BaseModel):
    intent: str
    reason: str
    w_ocr: float
    w_clip: float
    w_shadow: float


class WeightProfileUpdate(BaseModel):
    w_ocr: float = Field(ge=0)
    w_clip: float = Field(ge=0)
    w_shadow: float = Field(ge=0)

    @model_validator(mode="after")
    def require_positive_total(self) -> "WeightProfileUpdate":
        if self.w_ocr + self.w_clip + self.w_shadow <= 0:
            raise ValueError("at least one search weight must be greater than 0")
        return self


@router.get("/search/weights", response_model=list[WeightProfileResponse])
def list_weight_profiles(request: Request) -> list[WeightProfileResponse]:
    """List all persisted intent weight profiles."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        rows = session.scalars(select(SearchWeightProfile).order_by(
            SearchWeightProfile.intent, SearchWeightProfile.reason
        )).all()
        return [
            WeightProfileResponse(
                intent=row.intent, reason=row.reason,
                w_ocr=row.w_ocr, w_clip=row.w_clip, w_shadow=row.w_shadow,
            )
            for row in rows
        ]


@router.put("/search/weights/{intent}/{reason}", response_model=WeightProfileResponse)
def upsert_weight_profile(
    intent: str,
    reason: str,
    body: WeightProfileUpdate,
    request: Request,
) -> WeightProfileResponse:
    """Create or update a weight profile for a specific intent+reason pair.

    Example: PUT /search/weights/semantic/auto-travel
    Body: {"w_ocr": 0.03, "w_clip": 0.75, "w_shadow": 0.15}
    """
    database = require_state(request, "database")
    with database.session_factory() as session:
        existing = session.execute(
            select(SearchWeightProfile).where(
                SearchWeightProfile.intent == intent,
                SearchWeightProfile.reason == reason,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = SearchWeightProfile(intent=intent, reason=reason,
                                           w_ocr=body.w_ocr, w_clip=body.w_clip, w_shadow=body.w_shadow)
            session.add(existing)
        else:
            existing.w_ocr = body.w_ocr
            existing.w_clip = body.w_clip
            existing.w_shadow = body.w_shadow
        session.commit()
        session.refresh(existing)
        response = WeightProfileResponse(
            intent=existing.intent, reason=existing.reason,
            w_ocr=existing.w_ocr, w_clip=existing.w_clip, w_shadow=existing.w_shadow,
        )
    clear_query_cache()
    return response


@router.delete("/search/weights/{intent}/{reason}", status_code=204)
def delete_weight_profile(intent: str, reason: str, request: Request) -> None:
    """Delete a persisted weight profile (revert to built-in defaults)."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        row = session.execute(
            select(SearchWeightProfile).where(
                SearchWeightProfile.intent == intent,
                SearchWeightProfile.reason == reason,
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Weight profile not found")
        session.delete(row)
        session.commit()
    clear_query_cache()


# ---------------------------------------------------------------------------
# Search feedback API  (hide / promote / correct_tag)
# ---------------------------------------------------------------------------

_VALID_ACTIONS = {"hide", "promote", "correct_tag"}


class FeedbackRequest(BaseModel):
    file_id: str
    action: str                   # "hide" | "promote" | "correct_tag"
    query_hint: str = ""          # optional: scope feedback to a query
    tag_correction: Optional[str] = None


class FeedbackResponse(BaseModel):
    id: int
    file_id: str
    action: str
    query_hint: str
    tag_correction: Optional[str]


@router.post("/search/feedback", response_model=FeedbackResponse, status_code=201)
def add_feedback(body: FeedbackRequest, request: Request) -> FeedbackResponse:
    """Record user feedback for a search result.

    action='hide'        — permanently exclude this file from search results.
    action='promote'     — boost this file in future searches.
    action='correct_tag' — supply a corrected tag (tag_correction required).
    """
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=422, detail=f"action must be one of {sorted(_VALID_ACTIONS)}")
    if body.action == "correct_tag" and not body.tag_correction:
        raise HTTPException(status_code=422, detail="tag_correction is required for action='correct_tag'")

    database = require_state(request, "database")
    with database.session_factory() as session:
        existing = session.execute(
            select(SearchFeedback).where(
                SearchFeedback.file_id == body.file_id,
                SearchFeedback.action == body.action,
                SearchFeedback.query_hint == body.query_hint,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = SearchFeedback(
                file_id=body.file_id,
                action=body.action,
                query_hint=body.query_hint,
                tag_correction=body.tag_correction,
            )
            session.add(existing)
        else:
            existing.tag_correction = body.tag_correction
        session.commit()
        session.refresh(existing)
        response = FeedbackResponse(
            id=existing.id,
            file_id=existing.file_id,
            action=existing.action,
            query_hint=existing.query_hint,
            tag_correction=existing.tag_correction,
        )
    clear_query_cache()
    return response


@router.get("/search/feedback", response_model=list[FeedbackResponse])
def list_feedback(request: Request, action: Optional[str] = None) -> list[FeedbackResponse]:
    """List recorded search feedback, optionally filtered by action type."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        stmt = select(SearchFeedback).order_by(SearchFeedback.created_at.desc())
        if action:
            stmt = stmt.where(SearchFeedback.action == action)
        rows = session.scalars(stmt).all()
        return [
            FeedbackResponse(
                id=r.id, file_id=r.file_id, action=r.action,
                query_hint=r.query_hint, tag_correction=r.tag_correction,
            )
            for r in rows
        ]


@router.delete("/search/feedback/{feedback_id}", status_code=204)
def delete_feedback(feedback_id: int, request: Request) -> None:
    """Remove a feedback entry (undo hide/promote)."""
    database = require_state(request, "database")
    with database.session_factory() as session:
        row = session.get(SearchFeedback, feedback_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Feedback entry not found")
        session.delete(row)
        session.commit()
    clear_query_cache()


@router.get("/search/weights/defaults", response_model=list[WeightProfileResponse])
def list_default_weights(request: Request) -> list[WeightProfileResponse]:  # noqa: ARG001
    """Return the built-in default weights for all known intent+reason pairs."""
    combos = [
        ("ocr", "manual"), ("ocr", "auto-text-hint"), ("ocr", "auto-screen-text"),
        ("ocr", "auto-code"), ("ocr", "auto-word-match"), ("ocr", "auto-phrase-code"),
        ("semantic", "manual"), ("semantic", "auto-face"), ("semantic", "auto-travel"),
        ("semantic", "auto-celebration"),
        ("hybrid", "auto-mixed"), ("hybrid", "fallback"),
    ]
    return [
        WeightProfileResponse(
            intent=intent, reason=reason,
            **{f"w_{k}": v for k, v in intent_weights(intent, reason).items()},
        )
        for intent, reason in combos
    ]


# ---------------------------------------------------------------------------
# Index rebuild + health
# ---------------------------------------------------------------------------

@router.post("/search/index/rebuild", status_code=200)
def rebuild_search_index(request: Request) -> dict[str, Any]:
    """Rebuild FTS indexes and invalidate the vector index.

    Drops and recreates both FTS virtual tables, then flushes the in-memory
    query cache and marks the FAISS singleton for rebuild on next search.
    Use this after bulk imports or when index state is suspect.

    Note: this does NOT re-run semantic maintenance — use
    POST /scan/semantic-maintenance to re-derive missing search_documents.
    """
    database = require_state(request, "database")
    fts_rebuilt = False
    fts_ko_rebuilt = False

    with database.session_factory() as session:
        conn = session.connection()
        if conn.dialect.name == "sqlite":
            try:
                conn.execute(text("DROP TABLE IF EXISTS search_documents_fts"))
                conn.execute(text(
                    "CREATE VIRTUAL TABLE search_documents_fts "
                    "USING fts5(file_id UNINDEXED, search_text, keyword_text, semantic_text, tokenize='unicode61')"
                ))
                # Re-populate from search_documents
                conn.execute(text(
                    "INSERT INTO search_documents_fts(file_id, search_text, keyword_text, semantic_text) "
                    "SELECT file_id, search_text, keyword_text, semantic_text FROM search_documents"
                ))
                fts_rebuilt = True
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"FTS rebuild failed: {exc}") from exc

            try:
                conn.execute(text("DROP TABLE IF EXISTS search_documents_fts_ko"))
                conn.execute(text(
                    "CREATE VIRTUAL TABLE search_documents_fts_ko "
                    "USING fts5(file_id UNINDEXED, search_text, keyword_text, semantic_text, tokenize='trigram')"
                ))
                conn.execute(text(
                    "INSERT INTO search_documents_fts_ko(file_id, search_text, keyword_text, semantic_text) "
                    "SELECT file_id, search_text, keyword_text, semantic_text FROM search_documents"
                ))
                fts_ko_rebuilt = True
            except Exception:
                pass  # trigram unavailable on older SQLite — not an error

        session.commit()

    cache_cleared = clear_query_cache()
    vector_invalidated = invalidate_global_vector_index()

    return {
        "fts_rebuilt": fts_rebuilt,
        "fts_ko_rebuilt": fts_ko_rebuilt,
        "query_cache_cleared": cache_cleared,
        "vector_index_invalidated": vector_invalidated,
    }


@router.get("/search/index/status")
def search_index_status(request: Request) -> dict[str, Any]:
    """Return health and size metrics for all search index layers.

    Reports:
    - search_documents count
    - FTS row counts (unicode61 + trigram tables)
    - whether FAISS vector index is loaded
    - approximate vector count from media_embeddings
    """
    from app.models.semantic import MediaEmbedding
    from app.services.search.vector import _global_faiss_index

    database = require_state(request, "database")
    with database.session_factory() as session:
        doc_count = session.scalar(select(func.count()).select_from(SearchDocument)) or 0
        embedding_count = session.scalar(select(func.count()).select_from(MediaEmbedding)) or 0

        fts_count: int | None = None
        fts_ko_count: int | None = None
        conn = session.connection()
        if conn.dialect.name == "sqlite":
            try:
                fts_count = conn.execute(
                    text("SELECT COUNT(*) FROM search_documents_fts")
                ).scalar()
            except Exception:
                pass
            try:
                fts_ko_count = conn.execute(
                    text("SELECT COUNT(*) FROM search_documents_fts_ko")
                ).scalar()
            except Exception:
                pass

    faiss_loaded = _global_faiss_index is not None and _global_faiss_index._index is not None
    faiss_ntotal: int | None = None
    if faiss_loaded and _global_faiss_index is not None:
        faiss_ntotal = getattr(_global_faiss_index._index, "ntotal", None)

    return {
        "search_documents": doc_count,
        "embeddings": embedding_count,
        "fts": {
            "unicode61_rows": fts_count,
            "trigram_rows": fts_ko_count,
        },
        "vector_index": {
            "backend": "faiss" if _global_faiss_index is not None else "numpy",
            "loaded": faiss_loaded,
            "ntotal": faiss_ntotal,
        },
    }
