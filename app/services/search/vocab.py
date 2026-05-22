"""Dynamic tag vocabulary loaded from the user's own library.

Instead of maintaining hand-curated PLACE_TERMS / PERSON_TERMS dictionaries,
TagVocabularyCache reads the actual Tag rows from the database so the planner
automatically recognises every place and person the user has ever tagged —
including long-tail names like "여수밤바다", "에버랜드", "거제도" that would
never appear in a static list.

Usage
-----
    vocab = TagVocabularyCache(session)
    plan  = plan_query(query, tag_vocab=vocab.get())
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tag types that map to semantic categories
# ---------------------------------------------------------------------------
_PLACE_TAG_TYPES = {"place", "place_detail", "location", "geo", "geo_detail"}
_PERSON_TAG_TYPES = {"person", "people", "face", "auto_person"}
_VISUAL_TAG_TYPES = {
    "auto",
    "auto_scene",
    "auto_object",
    "auto_event",
    "auto_screen",
    "custom",
}

# Cache TTL: re-query the DB after this many seconds.
# 5 minutes is a good balance — new tags appear after semantic maintenance
# (~60s cycle) but we don't want per-request DB overhead.
_TTL_SECONDS = 300


@dataclass
class TagVocabulary:
    """Snapshot of tag values from the user's library, keyed by semantic role."""

    place_tags: frozenset[str] = field(default_factory=frozenset)
    person_tags: frozenset[str] = field(default_factory=frozenset)
    visual_tags: frozenset[str] = field(default_factory=frozenset)
    all_tags: frozenset[str] = field(default_factory=frozenset)

    def is_empty(self) -> bool:
        return not self.all_tags


_EMPTY_VOCAB = TagVocabulary()


class TagVocabularyCache:
    """Per-process TTL cache of tag vocabulary loaded from SQLAlchemy session.

    Thread-safe for read; concurrent refresh is serialised by _refresh_lock so
    only one thread issues the DB query when the TTL expires.
    """

    # Process-level singleton populated on first access per session factory.
    # Each SqlAlchemyHybridSearchBackend instance shares this across requests.
    _cache: TagVocabulary = _EMPTY_VOCAB
    _loaded_at: float = 0.0
    _refresh_lock: threading.Lock = threading.Lock()

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> TagVocabulary:
        """Return cached vocabulary, refreshing from DB when TTL has expired."""
        now = time.monotonic()
        if now - TagVocabularyCache._loaded_at < _TTL_SECONDS and not TagVocabularyCache._cache.is_empty():
            return TagVocabularyCache._cache
        with TagVocabularyCache._refresh_lock:
            # Re-check under lock: another thread may have already refreshed.
            now = time.monotonic()
            if now - TagVocabularyCache._loaded_at < _TTL_SECONDS and not TagVocabularyCache._cache.is_empty():
                return TagVocabularyCache._cache
            return self._refresh(now)

    @classmethod
    def invalidate(cls) -> None:
        """Force reload on next access (call after semantic maintenance).

        This is a classmethod so it can be called without a session instance:
            TagVocabularyCache.invalidate()
        """
        cls._loaded_at = 0.0
        cls._cache = _EMPTY_VOCAB

    def _refresh(self, now: float) -> TagVocabulary:
        try:
            from sqlalchemy import select
            from app.models.tag import Tag

            rows = self._session.execute(
                select(Tag.tag_type, Tag.tag_value).distinct()
            ).all()

            place_tags: set[str] = set()
            person_tags: set[str] = set()
            visual_tags: set[str] = set()
            all_tags: set[str] = set()

            for tag_type, tag_value in rows:
                if not tag_value:
                    continue
                v = str(tag_value).strip().casefold()
                all_tags.add(v)
                if tag_type in _PLACE_TAG_TYPES:
                    if _is_coordinate_tag(v):
                        continue
                    place_tags.add(v)
                elif tag_type in _PERSON_TAG_TYPES:
                    person_tags.add(v)
                elif tag_type in _VISUAL_TAG_TYPES:
                    visual_tags.add(v)

            vocab = TagVocabulary(
                place_tags=frozenset(place_tags),
                person_tags=frozenset(person_tags),
                visual_tags=frozenset(visual_tags),
                all_tags=frozenset(all_tags),
            )
            TagVocabularyCache._cache = vocab
            TagVocabularyCache._loaded_at = now
            logger.debug(
                "TagVocabularyCache refreshed: %d place, %d person, %d visual, %d total",
                len(place_tags), len(person_tags), len(visual_tags), len(all_tags),
            )
            return vocab
        except Exception as exc:
            logger.warning("TagVocabularyCache refresh failed: %s", exc)
            return TagVocabularyCache._cache


def _is_coordinate_tag(value: str) -> bool:
    return bool(re.match(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$", value.strip()))
