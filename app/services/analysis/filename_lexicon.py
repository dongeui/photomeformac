"""Load filename-derived auto-tag rules from packaged YAML."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
import re
from typing import Any, Pattern

import yaml

logger = logging.getLogger(__name__)
_FILENAME_TAGS_PATH = Path(__file__).with_name("filename_tags.yaml")


@dataclass(frozen=True)
class FilenameTagRule:
    pattern: Pattern[str]
    tag_value: str
    tag_type: str


@lru_cache(maxsize=1)
def _load_yaml() -> dict[str, Any]:
    try:
        with _FILENAME_TAGS_PATH.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning("filename_tags.yaml missing or invalid: %s", exc)
        return {}


@lru_cache(maxsize=1)
def load_filename_tag_rules() -> tuple[FilenameTagRule, ...]:
    raw = _load_yaml().get("rules") or []
    if not isinstance(raw, list):
        return ()

    rules: list[FilenameTagRule] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        pattern = str(row.get("pattern", "")).strip()
        tag_value = str(row.get("tag_value", "")).strip()
        tag_type = str(row.get("tag_type", "")).strip()
        if not (pattern and tag_value and tag_type):
            continue
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            logger.warning("invalid filename tag pattern %r: %s", pattern, exc)
            continue
        rules.append(FilenameTagRule(pattern=compiled, tag_value=tag_value, tag_type=tag_type))
    return tuple(rules)


def clear_filename_lexicon_cache() -> None:
    _load_yaml.cache_clear()
    load_filename_tag_rules.cache_clear()
