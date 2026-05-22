"""Load CLIP auto-tag concepts and aliases from packaged YAML."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
_CLIP_PATH = Path(__file__).with_name("clip_concepts.yaml")


@dataclass(frozen=True)
class ClipConcept:
    tag: str
    prompts: tuple[str, ...]
    threshold: float
    tag_type: str


@dataclass(frozen=True)
class ClipTaggingSettings:
    max_aliases_per_concept: int


def _cap_alias_tuple(canonical: str, aliases: list[str], cap: int) -> tuple[str, ...]:
    """Canonical first, then unique extras up to cap total length."""
    seen: list[str] = []
    for item in (canonical, *aliases):
        if item not in seen:
            seen.append(item)
        if len(seen) >= cap:
            break
    return tuple(seen)


@lru_cache(maxsize=1)
def clip_tagging_settings() -> ClipTaggingSettings:
    data = _load_yaml()
    raw = (data.get("settings") or {}).get("max_aliases_per_concept", 8)
    try:
        cap = max(1, int(raw))
    except (TypeError, ValueError):
        cap = 8
    return ClipTaggingSettings(max_aliases_per_concept=cap)


@lru_cache(maxsize=1)
def _load_yaml() -> dict[str, Any]:
    try:
        with _CLIP_PATH.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning("clip_concepts.yaml missing or invalid: %s", exc)
        return {}


@lru_cache(maxsize=1)
def load_clip_concepts() -> tuple[ClipConcept, ...]:
    data = _load_yaml()
    concepts_raw = data.get("concepts") or []
    cap = clip_tagging_settings().max_aliases_per_concept
    out: list[ClipConcept] = []
    if not isinstance(concepts_raw, list):
        return tuple(out)
    for row in concepts_raw:
        if not isinstance(row, dict):
            continue
        tag = str(row.get("tag", "")).strip()
        if not tag:
            continue
        prompts_raw = row.get("prompts") or []
        if not isinstance(prompts_raw, list):
            prompts_raw = []
        prompts = tuple(str(p).strip() for p in prompts_raw if str(p).strip())
        if not prompts:
            continue
        try:
            threshold = float(row.get("threshold", 0.25))
        except (TypeError, ValueError):
            threshold = 0.25
        tag_type = str(row.get("tag_type", "auto_scene")).strip() or "auto_scene"
        out.append(ClipConcept(tag=tag, prompts=prompts, threshold=threshold, tag_type=tag_type))
    if not out:
        logger.warning("No CLIP concepts loaded from %s; auto-tagging from embeddings disabled", _CLIP_PATH)
    return tuple(out)


@lru_cache(maxsize=1)
def load_concept_aliases() -> dict[str, tuple[str, ...]]:
    """Per-concept alias lists (length-capped) for persisted auto-tags."""
    data = _load_yaml()
    cap = clip_tagging_settings().max_aliases_per_concept
    raw = data.get("concept_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for key, val in raw.items():
        canonical = str(key).strip()
        if not canonical:
            continue
        if isinstance(val, list):
            aliases = [str(x).strip() for x in val if str(x).strip()]
        else:
            aliases = []
        result[canonical] = _cap_alias_tuple(canonical, aliases, cap)
    return result


@lru_cache(maxsize=1)
def concept_alias_clusters() -> tuple[frozenset[str], ...]:
    """Overlapping-merged clusters for merging into search tag_synonyms."""
    raw = _load_yaml().get("concept_aliases") or {}
    if not isinstance(raw, dict):
        return ()
    clusters: list[set[str]] = []
    for key, val in raw.items():
        k = str(key).strip()
        if not k:
            continue
        if isinstance(val, list):
            members = {k, *[str(x).strip() for x in val if str(x).strip()]}
        else:
            members = {k}
        merged: list[set[str]] = []
        pool = set(members)
        for existing in clusters:
            if existing & pool:
                pool |= existing
            else:
                merged.append(existing)
        merged.append(pool)
        clusters = merged
    return tuple(frozenset(c) for c in clusters)


def clear_clip_lexicon_cache() -> None:
    """Test helper: invalidate cached YAML."""
    _load_yaml.cache_clear()
    load_clip_concepts.cache_clear()
    load_concept_aliases.cache_clear()
    concept_alias_clusters.cache_clear()
    clip_tagging_settings.cache_clear()
