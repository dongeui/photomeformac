"""T24: Natural-language search regression scenarios.

Covers: parser fallback, empty-result handling, Korean mixed queries,
time expressions, compound condition fallback, face count, typo correction.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.search.hybrid import HybridSearchService
from app.services.search.planner import QueryPlan, plan_query


# ---------------------------------------------------------------------------
# Shared fake backends
# ---------------------------------------------------------------------------

class EmptyBackend:
    """Returns nothing from every channel."""

    def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
        return []

    def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
        return []

    def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
        return []

    def encode_text(self, query: str) -> bytes:
        return b""

    def suggest_related_tags(self, query: str, *, limit: int = 8) -> list[str]:
        return []


class SemanticOnlyBackend(EmptyBackend):
    """Returns a single result only from the embedding channel."""

    def encode_text(self, query: str) -> bytes:
        return b"vec"

    def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
        return [{"file_id": "sem-001", "distance": 0.15, "tags": []}]


class PlaceTagBackend(EmptyBackend):
    """Returns results tagged with a specific place when queried by place name."""

    _PLACE_MAP: dict[str, list[dict]] = {
        "제주": [{"file_id": "jeju-001", "tags": [{"type": "place", "value": "제주도"}], "distance": 0.2}],
        "부산": [{"file_id": "busan-001", "tags": [{"type": "place", "value": "부산광역시"}], "distance": 0.2}],
    }

    def encode_text(self, query: str) -> bytes:
        return b"vec"

    def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
        return []

    def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
        for place, results in self._PLACE_MAP.items():
            if place in query:
                return results
        return []


class TimeAwareBackend(EmptyBackend):
    """Returns results only via the shadow channel (no CLIP)."""

    def encode_text(self, query: str) -> bytes:
        return b""

    def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
        return [{"file_id": "time-001", "tags": [], "captured_at": "2024-08-15T12:00:00"}]


# ---------------------------------------------------------------------------
# T24-1: Parser fallback — degenerate / noise query returns gracefully
# ---------------------------------------------------------------------------

def test_nl_degenerate_query_returns_empty_gracefully() -> None:
    """Queries that are pure punctuation/whitespace should return [] without error."""
    service = HybridSearchService(EmptyBackend())

    results, meta = service.search_with_meta("   !!??  ", limit=5)

    assert results == []
    assert meta.get("effective_mode") is not None


# ---------------------------------------------------------------------------
# T24-2: Parser fallback — semantic-only fallback when parser has no intent match
# ---------------------------------------------------------------------------

def test_nl_unknown_query_falls_back_to_semantic_channel() -> None:
    """Unrecognised NL query without tags/places should still use semantic channel."""
    service = HybridSearchService(SemanticOnlyBackend())

    results, meta = service.search_with_meta("xyzzy plorp 무의미한 쿼리", limit=5)

    # The semantic (embedding) backend returned one result so final must be non-empty
    assert results, "expected semantic fallback to produce results"
    assert results[0]["file_id"] == "sem-001"


# ---------------------------------------------------------------------------
# T24-3: Empty result — no fallback loops, returns [] cleanly
# ---------------------------------------------------------------------------

def test_nl_empty_result_does_not_loop() -> None:
    """A query that finds nothing across all channels should return [] without recursion."""
    service = HybridSearchService(EmptyBackend())

    results, meta = service.search_with_meta("존재하지않는희귀한사진쿼리abc123", limit=5)

    assert results == []


# ---------------------------------------------------------------------------
# T24-4: Korean mixed query — time expression "작년" resolves to last year
# ---------------------------------------------------------------------------

def test_nl_time_expr_jagnyen_resolves_to_last_year() -> None:
    """'작년' in a query should produce date_from in the previous calendar year."""
    plan = plan_query("작년에 찍은 사진")

    assert plan.date_from is not None, "date_from should be set for '작년'"
    from datetime import date
    assert plan.date_from.year == date.today().year - 1


# ---------------------------------------------------------------------------
# T24-5: Korean mixed query — time expression "지난달" resolves to last month
# ---------------------------------------------------------------------------

def test_nl_time_expr_jidanhdal_resolves_to_last_month() -> None:
    """'지난달' should set date_from to the first day of the previous month."""
    plan = plan_query("지난달 사진 보여줘")

    assert plan.date_from is not None, "date_from should be set for '지난달'"
    today = date.today()
    expected_month = today.month - 1 if today.month > 1 else 12
    assert plan.date_from.month == expected_month


# ---------------------------------------------------------------------------
# T24-6: Korean mixed query — "2024년 여름" resolves to summer 2024
# ---------------------------------------------------------------------------

def test_nl_time_expr_2024_summer_resolves_correctly() -> None:
    """'2024년 여름' should produce date_from/date_to spanning Jun–Aug 2024."""
    plan = plan_query("2024년 여름 여행 사진")

    assert plan.date_from is not None, "date_from should be set for '2024년 여름'"
    assert plan.date_from.year == 2024
    assert plan.date_from.month in (6, 7), f"summer start unexpected: {plan.date_from}"
    if plan.date_to:
        assert plan.date_to.year == 2024
        assert plan.date_to.month in (8, 9), f"summer end unexpected: {plan.date_to}"


# ---------------------------------------------------------------------------
# T24-7: Compound condition fallback — place+person query falls back to place alone
# ---------------------------------------------------------------------------

def test_nl_compound_condition_fallback_to_place() -> None:
    """'제주에서 가족이랑' that finds nothing should fall back to '제주' alone."""

    class CompoundFallbackBackend(PlaceTagBackend):
        def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
            # Only "제주" alone (without person context) returns results
            if query.strip() == "제주":
                return [{"file_id": "jeju-001", "tags": [{"type": "place", "value": "제주도"}]}]
            return []

    service = HybridSearchService(CompoundFallbackBackend())

    results, meta = service.search_with_meta("제주에서 가족이랑 찍은 사진", limit=5)

    assert results, "expected condition fallback to find results for place term alone"
    assert results[0]["file_id"] == "jeju-001"
    assert meta.get("fallback") in ("condition_place_only", "condition_visual_only", "date_relaxed")


# ---------------------------------------------------------------------------
# T24-8: Mixed Korean-English query is handled without error
# ---------------------------------------------------------------------------

def test_nl_mixed_korean_english_query_runs_without_error() -> None:
    """Mixed Korean/English query like 'family photo 여행' should not crash."""
    service = HybridSearchService(SemanticOnlyBackend())

    results, meta = service.search_with_meta("family photo 여행", limit=5)

    assert isinstance(results, list)
    assert "effective_mode" in meta


# ---------------------------------------------------------------------------
# Cycle 1 additions — new time expressions
# ---------------------------------------------------------------------------

def test_nl_time_expr_geujeokke_resolves_to_day_before_yesterday() -> None:
    """'그저께' should set date_from/date_to to 2 days ago."""
    plan = plan_query("그저께 찍은 사진")
    from datetime import date, timedelta
    expected = date.today() - timedelta(days=2)
    assert plan.date_from == expected
    assert plan.date_to == expected


def test_nl_time_expr_n_il_jeon_resolves_correctly() -> None:
    """'3일 전' should set date to exactly 3 days ago."""
    from datetime import date, timedelta
    plan = plan_query("3일 전 사진")
    expected = date.today() - timedelta(days=3)
    assert plan.date_from == expected


def test_nl_time_expr_recent_n_days() -> None:
    """'최근 5일' should span last 5 days up to today."""
    from datetime import date, timedelta
    plan = plan_query("최근 5일 사진")
    assert plan.date_from is not None
    assert plan.date_to == date.today()
    assert (date.today() - plan.date_from).days == 5


def test_nl_time_expr_sangbankgi_resolves_to_first_half() -> None:
    """'올해 상반기' should span Jan–Jun of current year."""
    from datetime import date
    plan = plan_query("올해 상반기 여행 사진")
    assert plan.date_from is not None
    assert plan.date_from.month == 1
    assert plan.date_to is not None
    assert plan.date_to.month == 6


def test_nl_time_expr_habankgi_resolves_to_second_half() -> None:
    """'작년 하반기' should span Jul–Dec of last year."""
    from datetime import date
    plan = plan_query("작년 하반기 사진")
    assert plan.date_from is not None
    expected_year = date.today().year - 1
    assert plan.date_from.year == expected_year
    assert plan.date_from.month == 7
    assert plan.date_to is not None
    assert plan.date_to.month == 12


def test_nl_time_expr_yeonmal_resolves_to_year_end() -> None:
    """'연말' should span Nov–Dec of current year."""
    plan = plan_query("연말 파티 사진")
    assert plan.date_from is not None
    assert plan.date_from.month == 11


# ---------------------------------------------------------------------------
# Cycle 1 additions — compound face count
# ---------------------------------------------------------------------------

def test_nl_compound_duliseo_sets_face_exact_2() -> None:
    """'둘이서' should set face_count_exact=2."""
    plan = plan_query("둘이서 찍은 사진")
    assert plan.face_count_exact == 2


def test_nl_compound_setiseo_sets_face_exact_3() -> None:
    """'셋이서' should set face_count_exact=3."""
    plan = plan_query("셋이서 찍은 단체 사진")
    assert plan.face_count_exact == 3


def test_nl_compound_hamkke_sets_face_min_2() -> None:
    """'함께' should set face_count_min=2 (group shot context)."""
    plan = plan_query("가족이랑 함께 찍은 사진")
    assert plan.face_count_min == 2


# ---------------------------------------------------------------------------
# Cycle 1 additions — typo correction
# ---------------------------------------------------------------------------

def test_nl_typo_correction_냥이_to_고양이() -> None:
    """'냥이' should normalize to '고양이'."""
    from app.services.search.query_translate import normalize_query
    assert normalize_query("냥이 사진") == "고양이 사진"


def test_nl_typo_correction_멍멍이_to_강아지() -> None:
    """'멍멍이' should normalize to '강아지'."""
    from app.services.search.query_translate import normalize_query
    assert normalize_query("멍멍이랑 산책") == "강아지랑 산책"


def test_nl_typo_correction_카훼_to_카페() -> None:
    """'카훼' should normalize to '카페'."""
    from app.services.search.query_translate import normalize_query
    result = normalize_query("카훼 사진")
    assert "카페" in result


# ---------------------------------------------------------------------------
# Cycle 2 additions — extended time expressions
# ---------------------------------------------------------------------------

def test_nl_time_expr_최신사진_resolves_to_last_30_days() -> None:
    """'최신사진' compound should resolve to last 30 days."""
    from datetime import date, timedelta
    from app.services.search.query_translate import normalize_query, extract_date_range
    normalized = normalize_query("최신사진 보여줘")
    result = extract_date_range(normalized)
    assert result is not None
    date_from, date_to = result
    assert date_to == date.today()
    assert (date.today() - date_from).days <= 31


def test_nl_time_expr_n_gaeweol_jeon_resolves_correctly() -> None:
    """'3개월 전' should set date to ~3 months ago."""
    from datetime import date
    plan = plan_query("3개월 전 사진")
    assert plan.date_from is not None
    today = date.today()
    month3_ago = today.month - 3
    year = today.year
    if month3_ago <= 0:
        month3_ago += 12
        year -= 1
    assert plan.date_from.year == year
    assert plan.date_from.month == month3_ago


def test_nl_time_expr_idalmmal_resolves_to_end_of_month() -> None:
    """'이달말' should set date to last day of current month."""
    from datetime import date
    plan = plan_query("이달말 사진")
    assert plan.date_to is not None
    today = date.today()
    assert plan.date_to.month == today.month
    assert plan.date_to.year == today.year
    # date_to should be near end of month (day >= 28)
    assert plan.date_to.day >= 28


def test_nl_time_expr_idaalcho_resolves_to_start_of_month() -> None:
    """'이달초' should set date_from to first day of current month."""
    from datetime import date
    plan = plan_query("이달초 사진")
    assert plan.date_from is not None
    today = date.today()
    assert plan.date_from.day == 1
    assert plan.date_from.month == today.month


# ---------------------------------------------------------------------------
# Cycle 2 additions — generic scene term doesn't force place filter
# ---------------------------------------------------------------------------

def test_nl_generic_scene_term_does_not_force_place_filter() -> None:
    """'바다 사진' (generic scene) should NOT set require_place_match=True."""
    plan = plan_query("바다 사진")
    assert not plan.require_place_match, "generic scene '바다' should not force place filter"


def test_nl_compound_scene_plus_person_may_set_place_filter() -> None:
    """'제주에서 찍은 가족 사진' with specific place should set require_place_match."""
    plan = plan_query("제주에서 찍은 가족 사진")
    assert plan.require_place_match, "specific place '제주' with person should set place filter"


# ---------------------------------------------------------------------------
# Cycle 2 additions — token-safe typo correction
# ---------------------------------------------------------------------------

def test_nl_typo_no_false_substring_replacement() -> None:
    """Typo correction should not incorrectly alter tokens that only partially match."""
    from app.services.search.query_translate import normalize_query
    # '산책사진' should normalize but '산책' alone should remain '산책'
    result = normalize_query("강아지 산책 사진")
    assert "강아지" in result
    assert "산책" in result


def test_nl_typo_correction_멍뭉이_to_강아지() -> None:
    """'멍뭉이' should normalize to '강아지'."""
    from app.services.search.query_translate import normalize_query
    result = normalize_query("멍뭉이 사진")
    assert "강아지" in result


# ---------------------------------------------------------------------------
# Cycle 3 additions — English time expressions
# ---------------------------------------------------------------------------

def test_nl_time_expr_english_last_year() -> None:
    """'last year' should resolve to previous calendar year."""
    from datetime import date
    plan = plan_query("last year photos")
    assert plan.date_from is not None
    assert plan.date_from.year == date.today().year - 1
    assert plan.date_to is not None
    assert plan.date_to.year == date.today().year - 1


def test_nl_time_expr_english_last_month() -> None:
    """'last month' should resolve to previous calendar month."""
    from datetime import date
    plan = plan_query("photos from last month")
    assert plan.date_from is not None
    today = date.today()
    expected_month = today.month - 1 if today.month > 1 else 12
    assert plan.date_from.month == expected_month


def test_nl_time_expr_english_last_n_days() -> None:
    """'last 7 days' should span 7 days up to today."""
    from datetime import date, timedelta
    plan = plan_query("last 7 days photos")
    assert plan.date_from is not None
    assert plan.date_to == date.today()
    assert (date.today() - plan.date_from).days == 7


def test_nl_time_expr_english_yesterday() -> None:
    """'yesterday' should set date_from/date_to to yesterday."""
    from datetime import date, timedelta
    plan = plan_query("yesterday photos")
    expected = date.today() - timedelta(days=1)
    assert plan.date_from == expected


def test_nl_time_expr_english_this_year() -> None:
    """'this year' should start from Jan 1 of current year."""
    from datetime import date
    plan = plan_query("this year family photos")
    assert plan.date_from is not None
    assert plan.date_from.year == date.today().year
    assert plan.date_from.month == 1
    assert plan.date_from.day == 1


# ---------------------------------------------------------------------------
# Cycle 3 additions — English informal typo corrections
# ---------------------------------------------------------------------------

def test_nl_typo_english_bday_to_birthday() -> None:
    """'bday' should normalize to 'birthday'."""
    from app.services.search.query_translate import normalize_query
    result = normalize_query("bday party photos")
    assert "birthday" in result


def test_nl_typo_english_xmas_to_christmas() -> None:
    """'xmas' should normalize to 'christmas'."""
    from app.services.search.query_translate import normalize_query
    result = normalize_query("xmas photos")
    assert "christmas" in result


def test_nl_typo_english_vacay_to_vacation() -> None:
    """'vacay' should normalize to 'vacation'."""
    from app.services.search.query_translate import normalize_query
    result = normalize_query("vacay pics")
    assert "vacation" in result


# ---------------------------------------------------------------------------
# Cycle 3 additions — English face count patterns
# ---------------------------------------------------------------------------

def test_nl_face_count_english_group_photo() -> None:
    """'group photo' should set face_count_min=2."""
    plan = plan_query("group photo at the beach")
    assert plan.face_count_min == 2


def test_nl_face_count_english_just_two_of_us() -> None:
    """'just the two of us' should set face_count_exact=2."""
    plan = plan_query("just the two of us photo")
    assert plan.face_count_exact == 2


def test_nl_face_count_english_alone() -> None:
    """'alone' should set face_count_exact=1."""
    plan = plan_query("alone selfie photo")
    assert plan.face_count_exact == 1
