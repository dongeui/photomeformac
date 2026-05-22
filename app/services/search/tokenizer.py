"""Korean morphological tokenizer with graceful fallback.

When konlpy is installed (pip install photome[konlpy]), Okt extracts nouns
from Korean text — no dictionary required, handles any word the language
produces.  When konlpy is not available the module falls back to the
heuristic compound-token splitter already in planner.py.

Usage
-----
    from app.services.search.tokenizer import korean_nouns

    korean_nouns("제주도갔던사진")   # → ["제주도", "사진"]
    korean_nouns("여수밤바다 여행")  # → ["여수", "밤바다", "여행"]

Environment
-----------
    PHOTOME_TOKENIZER=okt     — force Okt (raises if konlpy missing)
    PHOTOME_TOKENIZER=mecab   — force Mecab (requires mecab-python3 + dict)
    PHOTOME_TOKENIZER=heuristic — force heuristic fallback (no konlpy needed)
    PHOTOME_TOKENIZER=auto    — (default) best available
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

_TOKENIZER_SETTING = os.environ.get("PHOTOME_TOKENIZER", "auto").casefold()

# Probe konlpy availability once at import time to avoid per-call warnings
_KONLPY_AVAILABLE: bool | None = None  # None = not yet checked


def _check_konlpy() -> bool:
    global _KONLPY_AVAILABLE
    if _KONLPY_AVAILABLE is None:
        try:
            import konlpy  # noqa: F401
            _KONLPY_AVAILABLE = True
        except ImportError:
            _KONLPY_AVAILABLE = False
            if _TOKENIZER_SETTING not in ("heuristic",):
                logger.info(
                    "konlpy not installed — using heuristic tokenizer. "
                    "Install with: pip install photome[konlpy]"
                )
    return bool(_KONLPY_AVAILABLE)

# ---------------------------------------------------------------------------
# Heuristic fallback (no external dependencies)
# ---------------------------------------------------------------------------
_KO_TRAILING_ENDINGS = (
    "에서의", "으로의", "로부터",
    "갔던", "찍었던", "찍은", "촬영한", "찍힌",
    "에서", "으로", "에게", "한테", "부터", "까지",
    "이랑", "하고", "와", "과",
    "이라는", "라는", "이라", "이고", "이며",
    "하는", "하던", "했던", "된", "되는", "되던",
    "있는", "있던", "없는",
    "여행",  # 바다여행 → 바다 + 여행
    "사진", "영상", "이미지", "그림", "컷",
    "에의", "의", "을", "를", "이", "가", "은", "는", "에", "로", "와", "과",
)

_KEEP_AS_TERM = {"여행", "사진", "영상", "이미지", "그림", "컷"}

# Particles that join two nouns in the middle of a compound token.
# Checked when no trailing ending matches, before giving up on splitting.
_INNER_JOINERS = ("이랑", "랑", "하고")


def _heuristic_split(token: str) -> list[str]:
    """Split a run-on Korean token into constituent nouns heuristically.

    Two passes:
    1. Strip known trailing particles/endings from the right end (loop).
    2. If no trailing ending found, look for inner joiner particles
       (이랑, 랑, 하고) and recurse on both sides.
    """
    result: list[str] = []
    remaining = token
    for _ in range(6):
        if not remaining or not re.search(r"[가-힣]", remaining):
            break
        split_here: str | None = None
        tail: str = ""
        for ending in _KO_TRAILING_ENDINGS:
            if remaining.endswith(ending) and len(remaining) > len(ending):
                core = remaining[: -len(ending)]
                if len(core) >= 2:
                    split_here = core
                    tail = ending
                    break
        if split_here:
            if tail in _KEEP_AS_TERM:
                result.insert(0, tail)
            remaining = split_here
        else:
            # Try inner joiner particles before giving up
            joined = False
            for joiner in _INNER_JOINERS:
                idx = remaining.find(joiner)
                if 0 < idx < len(remaining) - len(joiner):
                    left = remaining[:idx]
                    right = remaining[idx + len(joiner):]
                    if len(left) >= 2 and len(right) >= 2:
                        result = _heuristic_split(left) + _heuristic_split(right) + result
                        remaining = ""
                        joined = True
                        break
            if not joined:
                break
    if remaining and len(remaining) >= 2:
        result.insert(0, remaining)
    return result if result else [token]


def _heuristic_nouns(text: str) -> list[str]:
    """Extract noun-like tokens using heuristic splitting (no NLP library)."""
    raw = re.findall(r"[0-9A-Za-z가-힣_]+", text.casefold())
    tokens: list[str] = []
    for token in raw:
        # Lower threshold (4 vs 6) catches particle-attached tokens like "바다에서"
        if re.search(r"[가-힣]", token) and len(token) >= 4:
            tokens.extend(_heuristic_split(token))
        else:
            tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# KoNLPy — Okt (Open Korean Text)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _okt():
    from konlpy.tag import Okt  # type: ignore[import]
    return Okt()


def _okt_nouns(text: str) -> list[str]:
    try:
        okt = _okt()
        # pos() with norm=True, stem=True reduces surface forms
        pos_tags = okt.pos(text, norm=True, stem=True)
        # Keep nouns (NNP=고유명사, NNG=일반명사, Noun catchall)
        nouns = [
            word.casefold()
            for word, pos in pos_tags
            if pos in {"Noun", "NNP", "NNG"}
            and len(word) >= 2
        ]
        # Also include numbers/alphanumeric tokens
        alphanums = re.findall(r"[0-9A-Za-z_]+", text.casefold())
        return list(dict.fromkeys(nouns + alphanums))
    except Exception as exc:
        logger.warning("Okt tokenization failed, falling back: %s", exc)
        return _heuristic_nouns(text)


# ---------------------------------------------------------------------------
# KoNLPy — Mecab
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _mecab():
    from konlpy.tag import Mecab  # type: ignore[import]
    return Mecab()


def _mecab_nouns(text: str) -> list[str]:
    try:
        mecab = _mecab()
        pos_tags = mecab.pos(text)
        nouns = [
            word.casefold()
            for word, pos in pos_tags
            if pos.startswith("NN")  # NNG, NNP, NNB
            and len(word) >= 2
        ]
        alphanums = re.findall(r"[0-9A-Za-z_]+", text.casefold())
        return list(dict.fromkeys(nouns + alphanums))
    except Exception as exc:
        logger.warning("Mecab tokenization failed, falling back: %s", exc)
        return _heuristic_nouns(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def korean_nouns(text: str) -> list[str]:
    """Extract noun tokens from Korean (+ English/numeric) text.

    Returns deduplicated list of lowercase tokens suitable for
    planner._tokens() replacement.

    Priority:
      PHOTOME_TOKENIZER=mecab  → Mecab (best quality, requires install)
      PHOTOME_TOKENIZER=okt    → Okt   (good quality, pure Python)
      PHOTOME_TOKENIZER=auto   → try Okt, then Mecab, then heuristic
      PHOTOME_TOKENIZER=heuristic → heuristic only
    """
    if not text:
        return []

    if _TOKENIZER_SETTING == "heuristic":
        return _heuristic_nouns(text)

    if not _check_konlpy():
        return _heuristic_nouns(text)

    if _TOKENIZER_SETTING == "mecab":
        return _mecab_nouns(text)

    if _TOKENIZER_SETTING == "okt":
        return _okt_nouns(text)

    # auto: Okt → Mecab → heuristic
    try:
        return _okt_nouns(text)
    except Exception:
        pass
    try:
        return _mecab_nouns(text)
    except Exception:
        pass
    return _heuristic_nouns(text)


def is_morphological_tokenizer_available() -> bool:
    """Return True if konlpy (Okt or Mecab) is importable."""
    try:
        import konlpy  # noqa: F401
        return True
    except ImportError:
        return False
