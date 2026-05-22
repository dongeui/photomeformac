"""Automatic visual tags derived from analysis signals and CLIP embeddings."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
import re

import numpy as np

from app.core.contracts import MediaTagInput
from app.services.analysis.clip_lexicon import load_clip_concepts, load_concept_aliases
from app.services.analysis.filename_lexicon import (
    clear_filename_lexicon_cache,
    load_filename_tag_rules,
)
from app.services.embedding import clip as clip_embedding

# All tag_type values managed by this module.
# Import this in pipeline.py to bulk-replace auto tags without touching place/person/manual tags.
AUTO_TAG_TYPES: frozenset[str] = frozenset(
    {"auto_person", "auto_scene", "auto_object", "auto_event", "auto_screen"}
)

# EXIF month → season tag
_MONTH_SEASON: dict[int, str] = {
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
    12: "winter", 1: "winter", 2: "winter",
}


def tags_from_signals(analysis: dict, ocr_text: str = "") -> list[MediaTagInput]:
    tags: list[MediaTagInput] = []
    text = ocr_text.strip()
    if analysis.get("is_screenshot_like"):
        tags.append(MediaTagInput(tag_type="auto_screen", tag_value="screenshot"))
    if analysis.get("is_document_like"):
        tags.append(MediaTagInput(tag_type="auto_screen", tag_value="document"))
    if analysis.get("is_text_heavy") or text:
        tags.append(MediaTagInput(tag_type="auto_screen", tag_value="text"))
    if _looks_like_receipt(text):
        tags.append(MediaTagInput(tag_type="auto_screen", tag_value="receipt"))
    return tags


def tags_from_embedding_file(embedding_ref: str, embeddings_root: Path) -> list[MediaTagInput]:
    vector = _load_embedding_vector(embedding_ref, embeddings_root)
    if vector is None:
        return []
    return tags_from_embedding_vector(vector)


def tags_from_embedding_vector(vector: np.ndarray) -> list[MediaTagInput]:
    if vector.size == 0:
        return []
    normalized = _normalize(vector.astype("float32"))
    hits: list[tuple[float, str, str]] = []
    aliases_map = load_concept_aliases()
    for concept in load_clip_concepts():
        score = float(normalized.dot(_concept_vector(concept.tag)))
        if score >= concept.threshold:
            hits.append((score, concept.tag, concept.tag_type))

    hits.sort(reverse=True)
    seen: set[tuple[str, str]] = set()
    expanded: list[MediaTagInput] = []
    for _, tag, tag_type in hits[:5]:
        for alias in aliases_map.get(tag, (tag,)):
            key = (tag_type, alias)
            if key not in seen:
                seen.add(key)
                expanded.append(MediaTagInput(tag_type=tag_type, tag_value=alias))
        if len(expanded) >= 12:
            break
    return expanded


def merge_auto_tags(*tag_groups: list[MediaTagInput]) -> list[MediaTagInput]:
    seen: set[tuple[str, str]] = set()
    merged: list[MediaTagInput] = []
    for group in tag_groups:
        for tag in group:
            key = (tag.tag_type, tag.tag_value.casefold())
            if key in seen:
                continue
            seen.add(key)
            merged.append(tag)
    return merged


@lru_cache(maxsize=1)
def _concept_vectors() -> dict[str, np.ndarray]:
    clip_embedding.ensure_models()
    vectors: dict[str, np.ndarray] = {}
    for concept in load_clip_concepts():
        prompt_vectors = [
            clip_embedding.embedding_from_bytes(clip_embedding.encode_text(prompt))
            for prompt in concept.prompts
        ]
        vectors[concept.tag] = _normalize(np.mean(prompt_vectors, axis=0).astype("float32"))
    return vectors


def _concept_vector(tag: str) -> np.ndarray:
    return _concept_vectors()[tag]


def _load_embedding_vector(embedding_ref: str, embeddings_root: Path) -> np.ndarray | None:
    try:
        path = Path(embedding_ref)
        absolute_path = path if path.is_absolute() else embeddings_root / path.relative_to("embeddings")
        if not absolute_path.is_file():
            return None
        return np.load(absolute_path).astype("float32")
    except Exception:
        return None


def tags_from_filename(filename: str) -> list[MediaTagInput]:
    """Derive auto-tags from the filename (keyword matching, no ML)."""
    seen: set[tuple[str, str]] = set()
    tags: list[MediaTagInput] = []
    stem = Path(filename).stem
    for rule in load_filename_tag_rules():
        if rule.pattern.search(stem):
            key = (rule.tag_type, rule.tag_value)
            if key not in seen:
                seen.add(key)
                tags.append(MediaTagInput(tag_type=rule.tag_type, tag_value=rule.tag_value))
    return tags


def tags_from_datetime(dt: datetime) -> list[MediaTagInput]:
    """Add season auto-tag from EXIF capture datetime."""
    season = _MONTH_SEASON.get(dt.month)
    if season:
        return [MediaTagInput(tag_type="auto_scene", tag_value=season)]
    return []


def _looks_like_receipt(text: str) -> bool:
    lowered = text.casefold()
    hints = ("total", "subtotal", "receipt", "결제", "합계", "영수증", "카드", "승인")
    return any(hint in lowered for hint in hints)


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector)) or 1.0
    return vector / norm


def clear_auto_tag_caches() -> None:
    """Invalidate YAML-backed auto-tag caches.

    Intended for tests or an explicit admin reload path. In normal operation,
    restart the server after changing YAML and bump semantic_auto_tag_version
    when tag behavior changes.
    """
    from app.services.analysis.clip_lexicon import clear_clip_lexicon_cache
    from app.services.search.synonyms import clear_tag_synonyms_cache

    _concept_vectors.cache_clear()
    clear_clip_lexicon_cache()
    clear_filename_lexicon_cache()
    clear_tag_synonyms_cache()
