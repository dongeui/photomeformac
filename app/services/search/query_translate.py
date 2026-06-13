"""Query expansion helpers for Korean-first CLIP search."""
from __future__ import annotations

import os
import re
import time as _time
from datetime import date, timedelta
from functools import lru_cache

from app.services.search.seed import clip_templates, english_expansions, lexicon, typo_corrections

# ---------------------------------------------------------------------------
# Search seed dictionaries
# ---------------------------------------------------------------------------
LEXICON = lexicon()

# ---------------------------------------------------------------------------
# Typo / informal form corrections
# ---------------------------------------------------------------------------
TYPO_CORRECTIONS = typo_corrections()

# ---------------------------------------------------------------------------
# Filler words to strip from Korean natural language queries
# ---------------------------------------------------------------------------
_KO_FILLER = re.compile(
    r"(?:찍은|찍었던|찍힌|촬영한|촬영된|에서|에서의|에서 찍은|에서 찍|에서 본|에서 본)\s*사진|"
    r"찍은\s*사진|찍은\s*거|찍은\s*것|사진\s*좀|의\s*사진|이\s*사진|인\s*사진",
    re.UNICODE,
)

# Korean particles / endings to strip token-by-token.
# Longer suffixes must appear first to avoid partial matches.
_KO_PARTICLES_UNIQUE: tuple[str, ...] = (
    "에서의", "으로의", "로부터", "에서", "으로", "에게", "한테",
    "부터", "까지", "마다", "이나", "이랑", "하고",
    "이라는", "라는", "이라", "이고", "이며",
    "에의", "의", "을", "를", "이", "가", "은", "는", "에", "로", "와", "과",
)

# ---------------------------------------------------------------------------
# English expansions for common photo terms
# ---------------------------------------------------------------------------
ENGLISH_EXPANSIONS = english_expansions()


# ---------------------------------------------------------------------------
# CLIP natural-sentence templates
# Keyword → natural English sentence fragment for CLIP text encoder
# ---------------------------------------------------------------------------
_CLIP_TEMPLATES: dict[str, str] = clip_templates()


def _build_clip_sentence(query: str) -> str | None:
    """Compose a natural English sentence from matched template fragments.

    "가족 제주" → "a family together at Jeju island beach landscape Korea"
    Returns None if no templates matched.
    """
    lowered = query.casefold()
    matched: list[str] = []
    seen_text: set[str] = set()
    for keyword, fragment in _CLIP_TEMPLATES.items():
        if _keyword_matches(keyword, lowered) and fragment not in seen_text:
            matched.append(fragment)
            seen_text.add(fragment)
    if not matched:
        return None
    if len(matched) == 1:
        return matched[0]
    # Join first fragment as subject, rest with "at/with/and"
    parts = [matched[0]]
    for frag in matched[1:]:
        parts.append(frag)
    return " ".join(parts)


def expand_for_clip(query: str) -> list[str]:
    """Return original query plus optional English variants for CLIP."""
    cleaned = normalize_query(query)
    if not cleaned:
        return []

    # Strip filler phrases so CLIP gets the semantic core
    semantic_core = _KO_FILLER.sub("", cleaned).strip()
    if not semantic_core:
        semantic_core = cleaned

    variants = [semantic_core]

    # 1. Natural sentence template (most CLIP-friendly)
    sentence = _build_clip_sentence(cleaned)
    if sentence:
        variants.append(sentence)

    # 2. Lexicon-based keyword translation
    translated = translate_to_english(semantic_core)
    if translated and translated.casefold() != semantic_core.casefold():
        variants.append(translated)

    # 3. English term expansion
    english = _expand_english_terms(semantic_core)
    if english:
        variants.append(english)

    # Include original cleaned query if different from semantic_core
    if cleaned != semantic_core:
        variants.append(cleaned)

    return _dedupe(variants)


def extract_date_range(query: str) -> tuple[date | None, date | None]:
    """Extract an implicit date range from natural language Korean/English queries.

    Returns (date_from, date_to) or (None, None) when no time expression found.
    Recognized patterns:
    - 작년/올해/재작년, N년전
    - 이번달/지난달, N달전
    - 이번주/지난주/이번주말/지난주말, N주전
    - 봄/여름/가을/겨울 + year modifier
    - 오늘/어제/그저께, N일전
    - 최근 N일/N주
    - 1월~12월 specific month
    - 상반기/하반기, 연초/연말
    - 설날/추석 approximate window
    """
    today = date.today()
    year = today.year
    lowered = query.casefold().strip()

    # ── absolute: 오늘/today / 어제/yesterday / 그저께 ──
    if "오늘" in lowered or lowered == "today":
        return today, today
    if "그저께" in lowered or "그제" in lowered or "day before yesterday" in lowered:
        day = today - timedelta(days=2)
        return day, day
    if "어제" in lowered or "yesterday" in lowered:
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday

    # ── English: last N days/weeks ──
    m = re.search(r"last\s+([1-9]\d?)\s+days?", lowered)
    if m:
        return today - timedelta(days=int(m.group(1))), today
    m = re.search(r"last\s+([1-9]\d?)\s+weeks?", lowered)
    if m:
        return today - timedelta(weeks=int(m.group(1))), today
    m = re.search(r"past\s+([1-9]\d?)\s+days?", lowered)
    if m:
        return today - timedelta(days=int(m.group(1))), today
    if re.search(r"\b(this\s+week|this\s+month)\b", lowered):
        if "this week" in lowered:
            return today - timedelta(days=today.weekday()), today
        return today.replace(day=1), today
    if re.search(r"\b(last\s+week)\b", lowered):
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(weeks=1)
        return last_monday, this_monday - timedelta(days=1)
    if re.search(r"\b(last\s+month)\b", lowered):
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    if re.search(r"\b(last\s+year)\b", lowered):
        y = year - 1
        return date(y, 1, 1), date(y, 12, 31)
    if re.search(r"\b(this\s+year)\b", lowered):
        return date(year, 1, 1), today
    if re.search(r"\b(recently|recent\s+photos?)\b", lowered):
        return today - timedelta(days=30), today

    # ── 최근 N일 / 최근 N주 ──
    m = re.search(r"최근\s*([1-9]\d?)\s*일", lowered)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), today
    m = re.search(r"최근\s*([1-9]\d?)\s*주", lowered)
    if m:
        n = int(m.group(1))
        return today - timedelta(weeks=n), today
    if re.search(r"최근\s*(사진|찍은|일주일)", lowered):
        return today - timedelta(days=7), today
    if re.search(r"(최신|최신\s*사진|최근\s*사진)", lowered):
        return today - timedelta(days=30), today

    # ── N일전 ──
    m = re.search(r"([1-9]\d?)\s*일\s*전", lowered)
    if m:
        n = int(m.group(1))
        d = today - timedelta(days=n)
        return d, d

    # ── N년전 ──
    m = re.search(r"([1-9]\d?)년\s*전", lowered)
    if m:
        n = int(m.group(1))
        y = year - n
        # Check for a specific month: "1년전 7월" → ref_year로 그 해의 해당 월 반환
        mo_m = re.search(r"(1[0-2]|[1-9])월", lowered)
        if mo_m:
            import calendar
            mo = int(mo_m.group(1))
            last_day_num = calendar.monthrange(y, mo)[1]
            return date(y, mo, 1), date(y, mo, last_day_num)
        return date(y, 1, 1), date(y, 12, 31)

    # ── Explicit year mentions: "2023년", "23년" ──
    explicit = re.search(r"(20\d{2}|[2-9]\d)년", query)
    ref_year: int | None = None
    if explicit:
        raw = explicit.group(1)
        ref_year = int(raw) if len(raw) == 4 else 2000 + int(raw)

    # Year modifiers
    if "재작년" in lowered:
        ref_year = year - 2
    elif "작년" in lowered or "지난해" in lowered:
        ref_year = year - 1
    elif "올해" in lowered or "이번해" in lowered:
        ref_year = year

    # ── N달전 ──
    m = re.search(r"([1-9]\d?)달\s*전", lowered)
    if m:
        n = int(m.group(1))
        target = today.replace(day=1)
        for _ in range(n):
            target = (target - timedelta(days=1)).replace(day=1)
        last_day = (target.replace(month=target.month % 12 + 1, day=1) - timedelta(days=1)) if target.month < 12 else date(target.year, 12, 31)
        return target, last_day

    # ── N주전 ──
    m = re.search(r"([1-9]\d?)주\s*전", lowered)
    if m:
        n = int(m.group(1))
        week_start = today - timedelta(days=today.weekday() + 7 * n)
        week_end = week_start + timedelta(days=6)
        return week_start, min(week_end, today)

    # ── N개월전 (alias for N달전) ──
    m = re.search(r"([1-9]\d?)\s*개월\s*전", lowered)
    if m:
        n = int(m.group(1))
        target = today.replace(day=1)
        for _ in range(n):
            target = (target - timedelta(days=1)).replace(day=1)
        last_day = (target.replace(month=target.month % 12 + 1, day=1) - timedelta(days=1)) if target.month < 12 else date(target.year, 12, 31)
        return target, last_day

    # ── Month modifiers ──
    if "지난달" in lowered or "저번달" in lowered:
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    if "다음달" in lowered or "다음 달" in lowered:
        if today.month == 12:
            first_next = date(today.year + 1, 1, 1)
        else:
            first_next = today.replace(month=today.month + 1, day=1)
        import calendar
        last_next = date(first_next.year, first_next.month, calendar.monthrange(first_next.year, first_next.month)[1])
        return first_next, last_next
    if "이번달" in lowered or "이번 달" in lowered:
        return today.replace(day=1), today
    # 이달 초 / 이달 말
    if "이달초" in lowered or "이달 초" in lowered or "월초" in lowered:
        return today.replace(day=1), min(today, today.replace(day=10))
    if "이달말" in lowered or "이달 말" in lowered or "월말" in lowered:
        import calendar
        last = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=max(20, today.day)), today.replace(day=last)

    # ── Week modifiers ──
    if "이번주말" in lowered or "이번 주말" in lowered:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        sun = sat + timedelta(days=1)
        return sat, sun
    if "지난주말" in lowered or "저번주말" in lowered:
        last_sat = today - timedelta(days=today.weekday() + 2)
        last_sun = last_sat + timedelta(days=1)
        return last_sat, last_sun
    if "지난주" in lowered or "저번주" in lowered:
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(weeks=1)
        last_sunday = this_monday - timedelta(days=1)
        return last_monday, last_sunday
    if "이번주" in lowered or "이번 주" in lowered:
        start = today - timedelta(days=today.weekday())
        return start, today

    # ── Specific month: "3월", "12월" ──
    m = re.search(r"(1[0-2]|[1-9])월", lowered)
    if m:
        mo = int(m.group(1))
        y = ref_year or year
        import calendar
        last_day_num = calendar.monthrange(y, mo)[1]
        return date(y, mo, 1), date(y, mo, last_day_num)

    # ── Korean holidays (approximate windows) ──
    # 설날: late Jan ~ late Feb (lunar new year, varies)
    if "설날" in lowered or "설연휴" in lowered:
        y = ref_year or year
        return date(y, 1, 15), date(y, 2, 28)
    # 추석: mid Sep ~ mid Oct (chuseok, varies)
    if "추석" in lowered or "추석연휴" in lowered:
        y = ref_year or year
        return date(y, 9, 10), date(y, 10, 15)
    # 크리스마스: Dec 20~31
    if "크리스마스" in lowered or "성탄절" in lowered:
        y = ref_year or year
        return date(y, 12, 20), date(y, 12, 31)

    # ── Season mapping ──
    season_ranges: dict[str, tuple[int, int, int, int]] = {
        "봄": (3, 1, 5, 31),
        "spring": (3, 1, 5, 31),
        "여름": (6, 1, 8, 31),
        "summer": (6, 1, 8, 31),
        "가을": (9, 1, 11, 30),
        "fall": (9, 1, 11, 30),
        "autumn": (9, 1, 11, 30),
        "겨울": (12, 1, 2, 28),
        "winter": (12, 1, 2, 28),
    }
    for season_key, (sm, sd, em, ed) in season_ranges.items():
        if season_key in lowered:
            y = ref_year or year
            if sm > em:
                # Winter wraps year boundary: Dec(y-1) → Feb(y)
                # "작년 겨울" (ref_year=2025) → Dec 2024 ~ Feb 2025
                # "겨울" (no ref_year, y=current) → Dec(y-1) ~ Feb(y)
                start_year = (ref_year - 1) if ref_year is not None else (year - 1)
                date_from = date(start_year, sm, sd)
                date_to = date(y, em, ed)
            else:
                date_from = date(y, sm, sd)
                date_to = date(y, em, min(ed, 30 if em in (4, 6, 9, 11) else 31))
            return date_from, date_to

    # ── 상반기 / 하반기 ──
    if "상반기" in lowered or "전반기" in lowered:
        y = ref_year or year
        return date(y, 1, 1), date(y, 6, 30)
    if "하반기" in lowered or "후반기" in lowered:
        y = ref_year or year
        return date(y, 7, 1), date(y, 12, 31)

    # ── 연초 / 연말 ──
    if "연초" in lowered or "연말연시" in lowered:
        y = ref_year or year
        return date(y, 1, 1), date(y, 2, 28)
    if "연말" in lowered:
        y = ref_year or year
        return date(y, 11, 1), date(y, 12, 31)

    if ref_year is not None:
        return date(ref_year, 1, 1), date(ref_year, 12, 31)

    return None, None


def normalize_query(query: str) -> str:
    cleaned = re.sub(r"\s+", " ", query.strip())
    if not cleaned:
        return ""
    # Apply typo corrections token-by-token first to avoid partial substring hits
    # (e.g. "아가씨" must not match the "아가" rule inside a longer word)
    tokens = cleaned.split()
    corrected_tokens = []
    for tok in tokens:
        corrected_tokens.append(TYPO_CORRECTIONS.get(tok, tok))
    cleaned = " ".join(corrected_tokens)
    # Also apply substring corrections for multi-word compound entries (e.g. "비오는날")
    for typo, correction in TYPO_CORRECTIONS.items():
        if " " not in typo and typo not in cleaned:
            continue
        cleaned = cleaned.replace(typo, correction)
    cleaned = strip_korean_particles(cleaned)
    return cleaned.strip()


def strip_korean_particles(text: str) -> str:
    """Remove common Korean postpositional particles from each token.

    Only strips if the token contains Hangul and at least 2 characters
    remain after stripping, to avoid accidentally removing word endings
    that are part of the stem (e.g. "동의" should not become "동").
    """
    tokens = text.split()
    result = []
    for token in tokens:
        if _has_hangul(token):
            if token in LEXICON or token in _CLIP_TEMPLATES:
                result.append(token)
                continue
            for particle in _KO_PARTICLES_UNIQUE:
                candidate = token[: -len(particle)]
                if token.endswith(particle) and len(candidate) >= 2:
                    token = candidate
                    break
        result.append(token)
    return " ".join(result)


def _has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text))


def translate_to_english(query: str) -> str | None:
    """Translate Korean query to English when possible.

    The default path is a small deterministic lexicon, so the app stays fast and
    offline even before optional translation models are installed. Set
    TROVE_TRANSLATOR=opus to use a local HuggingFace MarianMT model when the
    dependencies and model cache are available.
    """
    if os.environ.get("TROVE_TRANSLATOR", "lexicon").casefold() == "opus":
        translated = _translate_with_opus(query)
        if translated:
            return translated
    return _translate_with_lexicon(query)


def _translate_with_lexicon(query: str) -> str | None:
    lowered = query.casefold()
    hits = [english for korean, english in LEXICON.items() if _keyword_matches(korean, lowered)]
    if not hits:
        return None
    return " ".join(hits)


def _keyword_matches(keyword: str, lowered_query: str) -> bool:
    lowered_keyword = keyword.casefold()
    if re.search(r"[a-z0-9]", lowered_keyword):
        if " " in lowered_keyword:
            return lowered_keyword in lowered_query
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(lowered_keyword)}(?![a-z0-9])", lowered_query))
    if len(lowered_keyword) <= 1:
        return lowered_keyword in lowered_query.split()
    return lowered_keyword in lowered_query


def _expand_english_terms(query: str) -> str | None:
    lowered = query.casefold()
    hits = [
        expanded
        for token, expanded in ENGLISH_EXPANSIONS.items()
        if re.search(rf"\b{re.escape(token)}\b", lowered)
    ]
    if not hits:
        return None
    return " ".join(hits)


@lru_cache(maxsize=1)
def _opus_pipeline():
    from transformers import MarianMTModel, MarianTokenizer

    model_name = os.environ.get("TROVE_TRANSLATOR_MODEL", "Helsinki-NLP/opus-mt-ko-en")
    tokenizer = MarianTokenizer.from_pretrained(model_name)
    model = MarianMTModel.from_pretrained(model_name)
    model.eval()
    return tokenizer, model


def _translate_with_opus(query: str) -> str | None:
    if not _has_hangul(query):
        return None
    try:
        import torch

        tokenizer, model = _opus_pipeline()
        tokens = tokenizer([query], return_tensors="pt", padding=True)
        with torch.no_grad():
            output = model.generate(**tokens, max_new_tokens=48)
        translated = tokenizer.decode(output[0], skip_special_tokens=True).strip()
        return translated or None
    except Exception:
        return None


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
