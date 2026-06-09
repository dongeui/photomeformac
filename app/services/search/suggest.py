"""Search autocomplete suggestions.

Lightweight, read-only helper that powers the search box dropdown.  It does NOT
instantiate the full hybrid backend (no embeddings / CLIP) — it only touches the
``tags`` and ``search_events`` tables so it stays fast enough to call on every
keystroke.

Two sources are merged, tags first:
  1. existing tag values (place / object / person names …) the user has indexed,
     ranked by prefix-match first, then by how many photos carry the tag.
  2. recent non-empty search queries the user actually ran (implicit history).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.semantic import SearchEvent
from app.models.tag import Tag

_LIKE_ESCAPE = "\\"
_MIN_QUERY_LEN = 1


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")


def autocomplete(session: Session, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Return up to ``limit`` suggestions for the in-progress query string.

    Each suggestion is ``{"value": str, "kind": "tag" | "recent", "count": int|None}``.
    Tag suggestions whose value begins with the query are ranked above mere
    substring matches; within each group the most-used tags come first.
    """
    lowered = query.casefold().strip()
    if len(lowered) < _MIN_QUERY_LEN:
        return []

    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()

    for value, count in _tag_candidates(session, lowered, limit=limit):
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        suggestions.append({"value": value, "kind": "tag", "count": int(count)})
        if len(suggestions) >= limit:
            return suggestions

    for value in _recent_queries(session, lowered, limit=limit):
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        suggestions.append({"value": value, "kind": "recent", "count": None})
        if len(suggestions) >= limit:
            break

    return suggestions


def _tag_candidates(session: Session, lowered: str, *, limit: int) -> list[tuple[str, int]]:
    substr_pat = f"%{_escape_like(lowered)}%"
    # Pull a generous candidate set then re-rank in Python so prefix matches win
    # without relying on non-standard "ORDER BY computed column" in GROUP BY.
    rows = session.execute(
        select(Tag.tag_value, func.count(Tag.file_id).label("freq"))
        .where(func.lower(Tag.tag_value).like(substr_pat, escape=_LIKE_ESCAPE))
        .where(Tag.tag_value.notlike("person-%"))  # hide internal person-id tags
        .group_by(Tag.tag_value)
        .order_by(func.count(Tag.file_id).desc())
        .limit(limit * 4)
    ).all()

    def sort_key(row: Any) -> tuple[int, int]:
        is_prefix = 0 if row.tag_value.casefold().startswith(lowered) else 1
        return (is_prefix, -int(row.freq))

    return [(row.tag_value, row.freq) for row in sorted(rows, key=sort_key)[:limit]]


def _recent_queries(session: Session, lowered: str, *, limit: int) -> list[str]:
    prefix_pat = f"{_escape_like(lowered)}%"
    rows = session.execute(
        select(SearchEvent.query, func.max(SearchEvent.created_at).label("last_run"))
        .where(func.lower(SearchEvent.query).like(prefix_pat, escape=_LIKE_ESCAPE))
        .where(SearchEvent.result_count > 0)
        .group_by(SearchEvent.query)
        .order_by(func.max(SearchEvent.created_at).desc())
        .limit(limit)
    ).all()
    return [row.query for row in rows if row.query and row.query.strip()]
