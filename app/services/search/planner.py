"""Rule-based query planning for image-first natural language search."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import chain
from datetime import date
import logging
import re
from typing import TYPE_CHECKING

from app.services.search import query_translate
from app.services.search.seed import seed_dict, seed_list

if TYPE_CHECKING:
    from app.services.search.vocab import TagVocabulary

logger = logging.getLogger(__name__)
PERSON_TERMS: set[str] = set(seed_list("person_terms"))
OCR_TERMS: set[str] = set(seed_list("ocr_terms"))
PLACE_TERMS: set[str] = set(seed_list("place_terms"))
VISUAL_TERMS: set[str] = set(seed_list("visual_terms"))
PLACE_ALIASES: dict[str, str] = seed_dict("place_aliases")

# Generic scene/natural feature words that appear in PLACE_TERMS but are NOT
# specific geographic locations. When these are the only place terms in a query
# they should not force require_place_match (scene tag, not address tag).
GENERIC_SCENE_TERMS: frozenset[str] = frozenset({
    "바다", "해변", "바닷가", "해수욕장", "해안",
    "산", "산속", "산꼭대기", "등산", "산길",
    "강", "강가", "강변", "호수", "계곡", "폭포",
    "숲", "숲속", "들판", "공원", "광장",
    "하늘", "구름", "beach", "sea", "ocean", "mountain", "river", "lake", "forest", "park",
})

DATE_STOP_TERMS = {
    "작년", "지난해", "재작년", "올해", "이번해",
    "지난달", "저번달", "이번달", "다음달", "이번", "달", "개월",
    "이달초", "이달말", "월초", "월말",
    "이번주", "지난주", "저번주", "이번주말", "지난주말", "저번주말", "주",
    "봄", "여름", "가을", "겨울", "spring", "summer", "fall", "autumn", "winter",
    "아침", "오전", "점심", "낮", "오후", "저녁", "밤", "야간", "새벽",
    "morning", "afternoon", "evening", "night", "dawn", "noon",
    "주말", "평일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "오늘", "어제", "그제", "그저께",
    "설날", "추석", "크리스마스", "성탄절",
    "상반기", "하반기", "전반기", "후반기", "연초", "연말", "연말연시",
    "최근", "일전", "며칠전", "날",
    "전", "전날",
    "없는", "없이", "말고", "제외", "빼고",
    # English time stop-words
    "yesterday", "today", "last year", "this year", "last month", "this month",
    "last week", "this week", "recently", "recent", "latest",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "xmas", "christmas",
}


@dataclass(frozen=True)
class QueryPlan:
    original_query: str
    normalized_query: str
    keyword_query: str
    visual_queries: list[str]
    date_from: date | None
    date_to: date | None
    person_terms: list[str]
    place_terms: list[str]
    ocr_terms: list[str]
    visual_terms: list[str]
    intent: str
    face_count_min: int | None = None
    face_count_max: int | None = None
    face_count_exact: int | None = None
    person_exclusive: bool = False
    require_all_persons: bool = False
    require_place_match: bool = False
    require_date_match: bool = False
    require_visual_match: bool = False
    excluded_terms: list[str] | None = None
    daypart: str | None = None
    allowed_weekdays: list[int] | None = None

    def to_meta(self) -> dict:
        return {
            "normalized_query": self.normalized_query,
            "keyword_query": self.keyword_query,
            "visual_queries": self.visual_queries,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "person_terms": self.person_terms,
            "place_terms": self.place_terms,
            "ocr_terms": self.ocr_terms,
            "visual_terms": self.visual_terms,
            "intent": self.intent,
            "face_count_min": self.face_count_min,
            "face_count_max": self.face_count_max,
            "face_count_exact": self.face_count_exact,
            "person_exclusive": self.person_exclusive,
            "require_all_persons": self.require_all_persons,
            "require_place_match": self.require_place_match,
            "require_date_match": self.require_date_match,
            "require_visual_match": self.require_visual_match,
            "excluded_terms": list(self.excluded_terms or []),
            "daypart": self.daypart,
            "allowed_weekdays": list(self.allowed_weekdays or []),
        }

    def has_face_count_filter(self) -> bool:
        return any(value is not None for value in (self.face_count_min, self.face_count_max, self.face_count_exact))

    def requires_person_match(self) -> bool:
        return bool(self.person_terms) and (
            self.person_exclusive
            or self.require_all_persons
            or self.has_face_count_filter()
            or self.require_place_match
            or self.require_date_match
            or self.require_visual_match
        )

    def has_hard_filters(self) -> bool:
        return any((
            self.has_face_count_filter(),
            self.requires_person_match(),
            self.require_place_match,
            self.require_date_match,
            self.require_visual_match,
            bool(self.excluded_terms),
            self.daypart is not None,
            bool(self.allowed_weekdays),
        ))

    def has_non_relaxable_filters(self) -> bool:
        return any((
            self.has_face_count_filter(),
            bool(self.excluded_terms),
            self.daypart is not None,
            bool(self.allowed_weekdays),
        ))


def plan_query(query: str, *, tag_vocab: "TagVocabulary | None" = None) -> QueryPlan:
    """Parse a natural-language query into a structured QueryPlan.

    tag_vocab: optional TagVocabulary loaded from the user's DB.  When provided,
    place/person matching is extended with every tag the user has ever created —
    so long-tail names like "여수밤바다" or "에버랜드" are recognised without
    any hardcoded dictionary entry.
    """
    normalized = query_translate.normalize_query(query)
    tokens = _tokens(normalized)
    date_from, date_to = query_translate.extract_date_range(normalized)
    exact_tag_query = _is_exact_tag_query(normalized, tokens, tag_vocab)
    if exact_tag_query:
        date_from = None
        date_to = None
    person_terms = _matching_terms_with_vocab(tokens, normalized, PERSON_TERMS,
                                              tag_vocab.person_tags if tag_vocab else None)
    person_terms = sorted(
        set(person_terms) | set(_internal_person_id_terms(query)),
        key=lambda item: (len(item), item),
    )
    if tag_vocab and tag_vocab.person_tags:
        raw_query = query.casefold().strip()
        raw_tokens = tokens + ([raw_query] if raw_query else [])
        person_terms = sorted(
            set(person_terms) | set(_matching_terms_with_vocab(raw_tokens, raw_query, PERSON_TERMS, tag_vocab.person_tags)),
            key=lambda item: (len(item), item),
        )
    place_terms = _matching_terms_with_vocab(tokens, normalized, PLACE_TERMS,
                                             tag_vocab.place_tags if tag_vocab else None)
    ocr_terms = _matching_terms(tokens, normalized, OCR_TERMS)
    visual_terms = _matching_terms_with_vocab(tokens, normalized, VISUAL_TERMS,
                                              tag_vocab.visual_tags if tag_vocab else None)
    excluded_terms = _extract_excluded_terms(query, person_terms, place_terms, ocr_terms, visual_terms)
    excluded_set = {term.casefold() for term in excluded_terms}
    person_terms = [term for term in person_terms if term.casefold() not in excluded_set]
    place_terms = [term for term in place_terms if term.casefold() not in excluded_set]
    ocr_terms = [term for term in ocr_terms if term.casefold() not in excluded_set]
    visual_terms = [term for term in visual_terms if term.casefold() not in excluded_set]
    visual_terms = _remove_visual_terms_embedded_in_places(visual_terms, place_terms)
    face_count_min, face_count_max, face_count_exact = _extract_face_count_constraint(normalized)
    daypart = _extract_daypart(normalized)
    allowed_weekdays = _extract_allowed_weekdays(normalized)
    if exact_tag_query:
        daypart = None
        allowed_weekdays = []
    keyword_tokens = [
        token
        for token in tokens
        if token not in DATE_STOP_TERMS and not _is_year_token(token) and token.casefold() not in excluded_set
    ]
    keyword_query = " ".join(keyword_tokens) if keyword_tokens else normalized
    if exact_tag_query:
        keyword_query = normalized
    visual_queries = query_translate.expand_for_clip(normalized)
    intent = _intent(
        person_terms=person_terms,
        place_terms=place_terms,
        ocr_terms=ocr_terms,
        visual_terms=visual_terms,
        date_from=date_from,
        keyword_query=keyword_query,
        normalized_query=normalized,
    )
    person_exclusive = _has_person_exclusive_marker(query, person_terms)
    # 인물 이름이 2명 이상 잡히면 자연어상 "함께"를 뜻한다(연결어 종류 무관).
    # 명시적 OR 표현이 있을 때만 합집합으로 본다.
    require_all_persons = len(person_terms) >= 2 and not _has_explicit_person_or(query)
    require_date_match = date_from is not None
    # Treat place_terms that fully overlap visual_terms as non-compound: e.g. "바다" appears
    # in both lists but it's a single-intent query, not a place+visual compound.
    place_set = {t.casefold() for t in place_terms}
    visual_set = {t.casefold() for t in visual_terms}
    distinct_place_terms = place_set - visual_set
    has_composite_visual = bool(visual_terms) and (
        bool(person_terms) or bool(distinct_place_terms) or require_date_match
    )
    # For require_place_match compound formula, only count non-generic place terms.
    specific_place_terms = [t for t in place_terms if t.casefold() not in GENERIC_SCENE_TERMS]
    specific_distinct_place_terms = {t.casefold() for t in distinct_place_terms if t.casefold() not in GENERIC_SCENE_TERMS}
    require_place_match = _requires_place_match(query, place_terms) or (
        bool(specific_distinct_place_terms) and (bool(person_terms) or bool(visual_terms) or require_date_match)
    ) or (
        bool(specific_place_terms) and bool(person_terms)
    )
    require_visual_match = has_composite_visual
    return QueryPlan(
        original_query=query,
        normalized_query=normalized,
        keyword_query=keyword_query,
        visual_queries=visual_queries,
        date_from=date_from,
        date_to=date_to,
        person_terms=person_terms,
        place_terms=place_terms,
        ocr_terms=ocr_terms,
        visual_terms=visual_terms,
        intent=intent,
        face_count_min=face_count_min,
        face_count_max=face_count_max,
        face_count_exact=face_count_exact,
        person_exclusive=person_exclusive,
        require_all_persons=require_all_persons,
        require_place_match=require_place_match,
        require_date_match=require_date_match,
        require_visual_match=require_visual_match,
        excluded_terms=excluded_terms,
        daypart=daypart,
        allowed_weekdays=allowed_weekdays,
    )


# 흔한 한국어 어미/조사 — 붙어있는 복합 토큰을 분해할 때 제거
_KO_TRAILING_ENDINGS = (
    "에서의", "으로의", "로부터",
    "갔던", "찍었던", "찍은", "촬영한", "찍힌",
    "에서", "으로", "에게", "한테", "부터", "까지",
    "이랑", "하고", "와", "과",
    "이라는", "라는", "이라", "이고", "이며",
    "하는", "하던", "했던", "된", "되는", "되던",
    "있는", "있던", "없는",
    "사진", "영상", "이미지", "그림", "컷",
    "에의", "의", "을", "를", "이", "가", "은", "는", "에", "로", "와", "과",
)


def _split_compound_token(token: str) -> list[str]:
    """공백 없이 붙어있는 복합 한글 토큰을 어미/조사 기준으로 분해.

    예: "제주도갔던사진" → ["제주도", "갔던", "사진"]
        "엄마랑" → ["엄마"]
        "서울에서" → ["서울"]
    의미 없는 잔여 토큰(1글자, 순수 어미)은 제거.
    """
    result: list[str] = []
    remaining = token
    # 최대 5회 반복으로 순차적으로 어미 분리
    for _ in range(5):
        if not remaining or not re.search(r"[가-힣]", remaining):
            break
        split_here: str | None = None
        for ending in _KO_TRAILING_ENDINGS:
            if remaining.endswith(ending) and len(remaining) > len(ending):
                core = remaining[: -len(ending)]
                if len(core) >= 2:
                    split_here = core
                    tail = ending
                    break
        if split_here:
            # 잘린 어미 자체도 의미 있으면 보존 (예: "사진", "영상")
            if tail in {"사진", "영상", "이미지", "그림", "컷"} and len(tail) >= 2:
                result.append(tail)
            remaining = split_here
        else:
            break
    if remaining and len(remaining) >= 2:
        result.insert(0, remaining)
    return result if result else [token]


def _tokens(query: str) -> list[str]:
    """Tokenize query into noun-level tokens.

    Uses the morphological tokenizer (KoNLPy Okt/Mecab) when available,
    falling back to the heuristic compound-token splitter.  Both paths
    are imported from tokenizer.py so this file has no direct NLP dependency.
    """
    from app.services.search.tokenizer import korean_nouns
    return korean_nouns(query)


def _matching_terms(tokens: list[str], normalized: str, terms: set[str]) -> list[str]:
    return _matching_terms_with_vocab(tokens, normalized, terms, None)


def _matching_terms_with_vocab(
    tokens: list[str],
    normalized: str,
    static_terms: set[str],
    dynamic_tags: "frozenset[str] | None",
) -> list[str]:
    """Match tokens/normalized query against static_terms + dynamic_tags from DB.

    dynamic_tags (from TagVocabularyCache) extends matching beyond the hardcoded
    static_terms so that user-created tags like "여수밤바다" or "에버랜드" are
    automatically detected without any dictionary entry.
    """
    lowered = normalized.casefold()
    hits: set[str] = set()

    # 1. Static vocabulary match (token exact or substring in normalized query)
    for term in static_terms:
        if _static_term_matches(term, tokens, lowered):
            hits.add(term)

    # 2. PLACE_ALIASES expansion for static vocab
    for token in tokens:
        canonical = PLACE_ALIASES.get(token)
        if canonical and canonical in static_terms:
            hits.add(canonical)
    for alias, canonical in PLACE_ALIASES.items():
        if alias in lowered and canonical in static_terms:
            hits.add(canonical)

    # 3. Dynamic tag vocabulary from DB — substring match within query tokens
    # Each DB tag value is checked against each query token (both directions):
    #   "제주" tag  + "제주도갔던" token → "제주" in token  → hit
    #   "에버랜드" tag + "에버랜드" token  → exact match      → hit
    if dynamic_tags:
        for tag in dynamic_tags:
            if not tag:
                continue
            # Exact token match or tag is a substring of a token (handles compound tokens)
            if tag in tokens:
                hits.add(tag)
            elif tag in lowered:
                hits.add(tag)
            else:
                # Check if any query token contains or is contained by the tag value
                for token in tokens:
                    if tag in token or (_allow_reverse_dynamic_tag_match(token, tag) and token in tag):
                        hits.add(tag)
                        break

    return sorted(hits, key=lambda item: (len(item), item))


def _allow_reverse_dynamic_tag_match(token: str, tag: str) -> bool:
    if len(token) < 3:
        return False
    if not re.search(r"[가-힣]", token + tag):
        return False
    if token in {"person", "people", "face", "auto"}:
        return False
    if re.fullmatch(r"\d+", token):
        return False
    if re.fullmatch(r"person-\d{6,}", tag):
        return False
    return True


def _remove_visual_terms_embedded_in_places(visual_terms: list[str], place_terms: list[str]) -> list[str]:
    if not visual_terms or not place_terms:
        return visual_terms
    place_set = {term.casefold() for term in place_terms if term}
    filtered: list[str] = []
    for term in visual_terms:
        lowered = term.casefold()
        if any(lowered != place and lowered in place for place in place_set):
            continue
        filtered.append(term)
    return filtered


def _internal_person_id_terms(query: str) -> list[str]:
    return sorted({match.group(0).casefold() for match in re.finditer(r"person-\d{6,}", query, re.IGNORECASE)})


def _is_exact_tag_query(
    normalized: str,
    tokens: list[str],
    tag_vocab: "TagVocabulary | None",
) -> bool:
    """Treat a bare existing DB tag as a tag lookup, not a date/season query."""
    if tag_vocab is None or tag_vocab.is_empty():
        return False
    lowered = normalized.casefold().strip()
    if not lowered:
        return False
    if lowered in tag_vocab.all_tags:
        return True
    return len(tokens) == 1 and tokens[0].casefold() in tag_vocab.all_tags


def _static_term_matches(term: str, tokens: list[str], lowered_query: str) -> bool:
    normalized_term = term.casefold()
    token_set = {token.casefold() for token in tokens}
    if normalized_term in token_set:
        return True

    if re.search(r"[a-z0-9]", normalized_term):
        if " " in normalized_term:
            return normalized_term in lowered_query
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])", lowered_query))

    if len(normalized_term) <= 1:
        return False
    return normalized_term in lowered_query


def _is_year_token(token: str) -> bool:
    return bool(re.fullmatch(r"(20\d{2}|[2-9]\d)년?", token))


_NUMBER_WORDS: dict[str, int] = {
    "한": 1,
    "하나": 1,
    "두": 2,
    "둘": 2,
    "세": 3,
    "셋": 3,
    "네": 4,
    "넷": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}

_COUNT_PATTERN = re.compile(
    r"(?P<prefix>정확히|딱)?\s*(?P<number>\d+|한|하나|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
    r"(?P<unit>명|사람|인|얼굴)"
)


def _extract_face_count_constraint(text: str) -> tuple[int | None, int | None, int | None]:
    lowered = text.casefold()
    if any(marker in lowered for marker in ("혼자", "단독", "solo", "alone", "by myself", "by herself", "by himself")):
        return None, None, 1
    # "둘이/둘이서" → exactly 2, "셋이/셋이서" → exactly 3, "넷이/넷이서" → 4
    if re.search(r"둘이|둘이서|둘\s*이서", lowered):
        return None, None, 2
    if re.search(r"셋이|셋이서|셋\s*이서", lowered):
        return None, None, 3
    if re.search(r"넷이|넷이서|넷\s*이서", lowered):
        return None, None, 4
    # English: "just the two of us", "the three of us"
    if re.search(r"\b(just\s+the\s+two|two\s+of\s+us)\b", lowered):
        return None, None, 2
    if re.search(r"\b(just\s+the\s+three|three\s+of\s+us)\b", lowered):
        return None, None, 3
    # "함께 찍은/같이 찍은" → at least 2 faces (explicitly in-frame together)
    if re.search(r"(함께|같이|다같이)\s*(찍|촬영)", lowered) or re.search(r"(together|group)\s+shot", lowered):
        return 2, None, None
    # English group/people patterns
    if re.search(r"\b(group\s+photo|group\s+pic|group\s+picture|everyone\s+together)\b", lowered):
        return 2, None, None

    for match in _COUNT_PATTERN.finditer(lowered):
        raw_number = match.group("number")
        parsed = _parse_count_value(raw_number)
        if parsed is None:
            continue
        prefix = match.group("prefix") or ""
        suffix = lowered[match.end(): match.end() + 16]
        if "이상" in suffix or "이거나 이상" in suffix:
            return parsed, None, None
        if any(token in suffix for token in ("초과", "넘게", "넘는", "넘은", "보다 많은")):
            return parsed + 1, None, None
        if "이하" in suffix:
            return None, parsed, None
        if any(token in suffix for token in ("미만", "보다 적은", "안되는")):
            return None, max(0, parsed - 1), None
        if prefix or any(token in suffix for token in ("만", "뿐")):
            return None, None, parsed
        return None, None, parsed
    return None, None, None


def _parse_count_value(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return _NUMBER_WORDS.get(value)


def _has_person_exclusive_marker(query: str, person_terms: list[str]) -> bool:
    lowered = query.casefold()
    for term in sorted(person_terms, key=len, reverse=True):
        if re.search(rf"{re.escape(term)}\s*(?:만|뿐)", lowered):
            return True
    return False


# 명시적 OR 표현. 이게 없으면 인물 다수는 기본 AND("함께")로 본다.
# "거나/이나"는 다른 맥락(찍거나, 얼마나 …)에서 오탐 위험이 있어 제외 — AND가
# 기본이므로 OR 마커를 보수적으로 잡아도 안전하다.
_PERSON_OR_MARKERS = ("또는", "혹은", " or ")


def _has_explicit_person_or(query: str) -> bool:
    lowered = query.casefold()
    return any(marker in lowered for marker in _PERSON_OR_MARKERS)


def _requires_place_match(query: str, place_terms: list[str]) -> bool:
    if not place_terms:
        return False
    # Generic scene words (바다, 산, 강 …) with "에서" still name a scene, not an address.
    # Only force place match when at least one term is a specific named location.
    specific_places = [t for t in place_terms if t.casefold() not in GENERIC_SCENE_TERMS]
    if not specific_places:
        return False
    lowered = query.casefold()
    return bool(re.search(
        r"(에서|에서의|에서\s*찍|에서\s*촬영|\bin\b|\bat\b|\bnear\b|\bfrom\b|\btaken\s+in\b|\bshot\s+in\b)",
        lowered,
    ))


_EXCLUSION_MARKERS = ("없이", "없는", "말고", "제외", "빼고", "without", "except", "excluding")

_DAYPART_ALIASES: dict[str, tuple[str, ...]] = {
    "dawn": ("새벽", "이른아침", "dawn"),
    "morning": ("아침", "오전", "morning"),
    "noon": ("점심", "정오", "noon"),
    "afternoon": ("낮", "오후", "afternoon"),
    "evening": ("저녁", "evening"),
    "night": ("밤", "야간", "심야", "night"),
}

_WEEKDAY_ALIASES: dict[int, tuple[str, ...]] = {
    0: ("월요일", "월", "monday", "mon"),
    1: ("화요일", "화", "tuesday", "tue"),
    2: ("수요일", "수", "wednesday", "wed"),
    3: ("목요일", "목", "thursday", "thu"),
    4: ("금요일", "금", "friday", "fri"),
    5: ("토요일", "토", "saturday", "sat"),
    6: ("일요일", "일", "sunday", "sun"),
}


def _extract_excluded_terms(
    query: str,
    person_terms: list[str],
    place_terms: list[str],
    ocr_terms: list[str],
    visual_terms: list[str],
) -> list[str]:
    lowered = query.casefold()
    excluded: list[str] = []
    candidates = sorted(
        {term.casefold() for term in chain(person_terms, place_terms, ocr_terms, visual_terms) if term},
        key=len,
        reverse=True,
    )
    for term in candidates:
        if any(re.search(rf"{re.escape(term)}\s*{marker}", lowered) for marker in _EXCLUSION_MARKERS):
            excluded.append(term)
    return list(dict.fromkeys(excluded))


def _extract_daypart(query: str) -> str | None:
    lowered = query.casefold()
    for canonical, aliases in _DAYPART_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return canonical
    return None


def _extract_allowed_weekdays(query: str) -> list[int]:
    lowered = query.casefold()
    if "주말" in lowered or "weekend" in lowered:
        return [5, 6]
    if "평일" in lowered or "weekday" in lowered:
        return [0, 1, 2, 3, 4]

    matched: list[int] = []
    for weekday, aliases in _WEEKDAY_ALIASES.items():
        for alias in aliases:
            if len(alias) == 1:
                if re.search(rf"{re.escape(alias)}요일", lowered):
                    matched.append(weekday)
                    break
                continue
            if alias in lowered:
                matched.append(weekday)
                break
    return sorted(dict.fromkeys(matched))


def _intent(
    *,
    person_terms: list[str],
    place_terms: list[str],
    ocr_terms: list[str],
    visual_terms: list[str],
    date_from: date | None,
    keyword_query: str,
    normalized_query: str,
) -> str:
    has_text = bool(ocr_terms)
    has_person = bool(person_terms)
    has_place = bool(place_terms)
    has_visual = bool(visual_terms or has_person or has_place or date_from)

    # OCR + visual signals together → mixed (e.g. "엄마 카톡 오류")
    if has_text and has_visual:
        return "mixed"
    if has_text:
        return "ocr"

    # Person + place together → visual (e.g. "엄마랑 카페")
    if has_person and has_place:
        return "visual"
    # Date + person or place → visual (e.g. "작년 제주 가족")
    if date_from and (has_person or has_place):
        return "visual"

    if has_visual:
        return "visual"
    if keyword_query != normalized_query:
        return "keyword"
    return "hybrid"
