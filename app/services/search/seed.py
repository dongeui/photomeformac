"""Packaged search seed data loader.

The base build depends on this module too, so failures must degrade to empty
data instead of breaking app startup. Edit ``vocab_seed.yaml`` to tune search
vocabulary without code changes.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_VOCAB_SEED_PATH = Path(__file__).with_name("vocab_seed.yaml")


@lru_cache(maxsize=1)
def load_search_seed() -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        logger.warning("PyYAML unavailable; search seed vocabulary disabled: %s", exc)
        return {}
    try:
        with _VOCAB_SEED_PATH.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to load search seed vocabulary from %s: %s", _VOCAB_SEED_PATH, exc)
        return {}


def seed_list(name: str) -> list[str]:
    value = load_search_seed().get(name, [])
    if not isinstance(value, list):
        logger.warning("Search seed field %s is not a list; ignoring it", name)
        return []
    return [str(item) for item in value]


def seed_dict(name: str) -> dict[str, str]:
    value = load_search_seed().get(name, {})
    if not isinstance(value, dict):
        logger.warning("Search seed field %s is not a mapping; ignoring it", name)
        return {}
    return {str(key): str(item) for key, item in value.items()}


def lexicon() -> dict[str, str]:
    return seed_dict("lexicon")


def typo_corrections() -> dict[str, str]:
    return seed_dict("typo_corrections")


def english_expansions() -> dict[str, str]:
    return seed_dict("english_expansions")


def clip_templates() -> dict[str, str]:
    return seed_dict("clip_templates")
