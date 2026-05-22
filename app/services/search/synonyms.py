"""Tag synonym map for hybrid search (YAML + CLIP alias clusters)."""

from __future__ import annotations

from functools import lru_cache
import logging
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)
_TAG_SYNONYMS_PATH = Path(__file__).with_name("tag_synonyms.yaml")


def _apply_symmetric_clusters(merged: dict[str, set[str]], cluster: frozenset[str]) -> None:
    for term in cluster:
        merged.setdefault(term, set()).update(cluster - {term})


@lru_cache(maxsize=1)
def load_tag_synonyms() -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    try:
        with _TAG_SYNONYMS_PATH.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning("tag_synonyms.yaml missing or invalid: %s", exc)
        raw = {}
    if isinstance(raw, dict):
        for key, vals in raw.items():
            k = str(key).strip()
            if not k:
                continue
            if isinstance(vals, list):
                merged[k] = {str(v).strip() for v in vals if str(v).strip()}
            else:
                merged[k] = set()
    try:
        from app.services.analysis.clip_lexicon import concept_alias_clusters

        for cluster in concept_alias_clusters():
            _apply_symmetric_clusters(merged, cluster)
    except Exception as exc:
        logger.debug("CLIP alias merge skipped: %s", exc)
    return merged


def clear_tag_synonyms_cache() -> None:
    load_tag_synonyms.cache_clear()


def tag_synonyms_snapshot() -> dict[str, set[str]]:
    """Mutable copy for callers that need to mutate."""
    return {k: set(v) for k, v in load_tag_synonyms().items()}
