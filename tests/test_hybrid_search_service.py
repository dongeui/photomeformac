from __future__ import annotations

from importlib import resources

from app.services.search.backend import SqlAlchemyHybridSearchBackend
from app.services.search.hybrid import (
    FeedbackReranker,
    HybridSearchService,
    apply_diversity_cap,
    apply_hard_filters,
    apply_single_person_solo_priority,
    resolve_effective_mode,
    search_sort_key,
)
from app.services.search.planner import QueryPlan, plan_query
from app.services.search.query_translate import expand_for_clip, normalize_query
from app.services.search.synonyms import load_tag_synonyms
from app.services.search.tokenizer import korean_nouns
from app.services.search.vocab import TagVocabulary


class FakeBackend:
    def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
        if query == "동의":
            return [
                {
                    "file_id": "file-a",
                    "ocr_text": "동의 필요",
                    "ocr_match_kind": "word",
                }
            ]
        return []

    def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
        if query_embedding == b"face":
            return [{"file_id": "face-file", "distance": 0.1}]
        return [
            {"file_id": "file-a", "distance": 0.2},
            {"file_id": "file-b", "distance": 0.4},
        ]

    def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
        if query == "baby":
            return [{"file_id": "auto-baby", "tag_exact_match": True, "tags": [{"type": "auto", "value": "baby"}]}]
        return []

    def encode_text(self, query: str) -> bytes:
        if "face" in query:
            return b"face"
        return b"embedding"

    def suggest_related_tags(self, query: str, *, limit: int = 8) -> list[str]:
        return []


class ReverseReranker:
    def rerank(self, results: list[dict], plan) -> list[dict]:  # noqa: ANN001
        return list(reversed(results))


class GoldenChannelBackend(FakeBackend):
    def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
        if query == "영수증 오류":
            return [{"file_id": "receipt-file", "ocr_text": "영수증 오류", "ocr_match_kind": "word"}]
        return []

    def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
        return [{"file_id": "beach-trip", "distance": 0.1, "tags": [
            {"type": "place", "value": "바다"},
            {"type": "auto_scene", "value": "beach"},
        ]}]

    def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
        if query == "바다 여행":
            return [
                {
                    "file_id": "beach-trip",
                    "tags": [{"type": "auto_scene", "value": "beach"}],
                    "tag_exact_match": True,
                }
            ]
        if query == "영수증 오류":
            return [
                {
                    "file_id": "receipt-file",
                    "tags": [{"type": "auto_screen", "value": "receipt"}],
                    "tag_exact_match": True,
                }
            ]
        return []


def test_korean_query_expands_for_clip() -> None:
    variants = expand_for_clip("자전거")

    assert variants[0] == "자전거"
    assert any("bicycle" in variant for variant in variants)


def test_search_seed_vocabulary_is_packaged() -> None:
    seed = resources.files("app.services.search").joinpath("vocab_seed.yaml")

    assert seed.is_file()
    assert "visual_terms:" in seed.read_text(encoding="utf-8")


def test_typo_query_normalizes_before_expansion() -> None:
    assert normalize_query("어르굴") == "얼굴"
    variants = expand_for_clip("어르굴")

    assert variants[0] == "얼굴"
    assert any("face" in variant for variant in variants)


def test_baby_and_woman_queries_expand_for_clip() -> None:
    assert any("baby" in variant for variant in expand_for_clip("아기"))
    assert any("woman" in variant for variant in expand_for_clip("여자"))
    assert any("baby" in variant for variant in expand_for_clip("baby"))
    assert any("woman" in variant for variant in expand_for_clip("woman"))


def test_effective_mode_routes_short_ocr_hits_to_ocr() -> None:
    mode, reason = resolve_effective_mode(
        "동의",
        "hybrid",
        [{"file_id": "file-a", "ocr_match_kind": "word"}],
    )

    assert mode == "ocr"
    assert reason == "auto-word-match"


def test_query_planner_extracts_image_search_intents() -> None:
    plan = plan_query("작년 여름 바다에서 가족이랑 찍은 사진")

    assert plan.intent == "visual"
    assert plan.date_from is not None
    assert "바다" in plan.place_terms
    assert "가족" in plan.person_terms
    assert any("sea" in variant or "ocean" in variant for variant in plan.visual_queries)


def test_exact_existing_tag_query_does_not_become_date_filter() -> None:
    plan = plan_query(
        "winter",
        tag_vocab=TagVocabulary(all_tags=frozenset({"winter"})),
    )

    assert plan.date_from is None
    assert plan.date_to is None
    assert plan.require_date_match is False


def test_exact_existing_daypart_tag_query_does_not_become_time_filter() -> None:
    plan = plan_query(
        "night",
        tag_vocab=TagVocabulary(all_tags=frozenset({"night"})),
    )

    assert plan.daypart is None
    assert plan.allowed_weekdays == []


def test_internal_person_id_query_matches_only_exact_dynamic_tag() -> None:
    plan = plan_query(
        "person-000059",
        tag_vocab=TagVocabulary(
            person_tags=frozenset({"person-000003", "person-000059", "person-000110"}),
            all_tags=frozenset({"person-000003", "person-000059", "person-000110"}),
        ),
    )

    assert plan.keyword_query == "person-000059"
    assert "person-000059" in plan.person_terms
    assert "person-000003" not in plan.person_terms
    assert "person-000110" not in plan.person_terms


def test_internal_person_id_survives_compound_query_tokenization() -> None:
    plan = plan_query(
        "person-000059랑 2024년에 찍은 사진",
        tag_vocab=TagVocabulary(
            person_tags=frozenset({"person-000003", "person-000059", "person-000110"}),
            all_tags=frozenset({"person-000003", "person-000059", "person-000110"}),
        ),
    )

    assert "person-000059" in plan.person_terms
    assert plan.requires_person_match() is True


def test_seaside_natural_language_query_expands_to_visual_search() -> None:
    plan = plan_query("바닷가에서 찍은 사진")

    assert plan.intent == "visual"
    assert "바닷가" in plan.place_terms
    assert any("seaside" in variant or "ocean" in variant for variant in plan.visual_queries)


def test_country_place_aliases_route_to_existing_geocode_tags() -> None:
    swiss = plan_query("스위스에서 찍은 사진")
    korea = plan_query("한국")
    japan = plan_query("일본 여행")

    assert "Schweiz/Suisse/Svizzera/Svizra" in swiss.place_terms
    assert swiss.require_place_match is True
    assert "대한민국" in korea.place_terms
    assert "日本" in japan.place_terms
    assert any("Switzerland" in variant for variant in swiss.visual_queries)


def test_country_tag_synonyms_match_reverse_geocode_outputs() -> None:
    synonyms = load_tag_synonyms()

    assert "대한민국" in synonyms["한국"]
    assert "日本" in synonyms["일본"]
    assert "Schweiz/Suisse/Svizzera/Svizra" in synonyms["스위스"]


def test_contextual_lifestyle_queries_route_to_visual_search() -> None:
    cases = [
        ("아이랑 놀이터에서 찍은 사진", ("아이", "놀이터"), ("children", "playground")),
        ("분위기 좋은 카페 데이트", ("데이트", "카페"), ("date", "cafe")),
        ("비오는 날 거리", ("비", "거리"), ("rain", "street")),
        ("맛집에서 먹은 사진", ("맛집",), ("restaurant", "food")),
        ("hotel pool vacation", ("hotel", "pool"), ("hotel", "pool")),
        ("vacation photos", ("vacation",), ("vacation",)),
    ]

    for query, expected_terms, expected_words in cases:
        plan = plan_query(query)
        haystack = " ".join(plan.visual_queries).casefold()
        matched_terms = set(plan.visual_terms) | set(plan.place_terms) | set(plan.person_terms)

        assert plan.intent in {"visual", "mixed"}
        assert any(term in matched_terms for term in expected_terms)
        assert all(word in haystack for word in expected_words)


def test_visual_terms_do_not_match_inside_unrelated_words() -> None:
    vacation = plan_query("hotel pool vacation")
    airplane = plan_query("비행기에서 찍은 사진")

    assert "cat" not in vacation.visual_terms
    assert "비" not in airplane.visual_terms
    assert "비" in plan_query("비오는 날 거리").visual_terms
    assert "비행기" in airplane.visual_terms
    assert not any("rain" in variant for variant in airplane.visual_queries)


def test_scene_tags_do_not_become_place_filters() -> None:
    child = plan_query("아이랑 놀이터에서 찍은 사진")
    airplane = plan_query("비행기에서 찍은 사진")

    assert "아이" not in child.place_terms
    assert "비" not in airplane.place_terms
    assert airplane.require_place_match is False


def test_contextual_tag_synonyms_bridge_user_language() -> None:
    synonyms = load_tag_synonyms()

    assert "restaurant" in synonyms["맛집"]
    assert "hotel" in synonyms["숙소"]
    assert "airplane" in synonyms["비행기"]
    assert "playground" in synonyms["놀이터"]
    assert "smiling" in synonyms["웃고있는"]


def test_hybrid_search_prefers_cross_channel_agreement() -> None:
    service = HybridSearchService(FakeBackend())

    results, meta = service.search_with_meta("동의", limit=5, mode="hybrid")

    assert meta["effective_mode"] == "ocr"
    assert meta["query_plan"]["intent"] == "hybrid"
    assert results[0]["file_id"] == "file-a"
    assert results[0]["match_reason"] == "ocr"
    assert results[0]["rank_score"] == 1.0


def test_face_query_routes_to_semantic() -> None:
    service = HybridSearchService(FakeBackend())

    results, meta = service.search_with_meta("남자 얼굴", limit=5, mode="hybrid")

    assert meta["effective_mode"] == "semantic"
    assert results[0]["file_id"] == "face-file"
    assert results[0]["match_reason"] == "clip"


def test_person_alias_with_stripped_particle_still_routes_to_auto_face() -> None:
    mode, reason = resolve_effective_mode(
        normalize_query("방울이"),
        "hybrid",
        [],
        planner_intent="visual",
        tag_vocab=TagVocabulary(
            person_tags=frozenset({"박지호", "방울이", "깜찍이"}),
            all_tags=frozenset({"박지호", "방울이", "깜찍이"}),
        ),
    )

    assert mode == "semantic"
    assert reason == "auto-face"


def test_place_alias_with_stripped_particle_still_routes_to_auto_travel() -> None:
    mode, reason = resolve_effective_mode(
        normalize_query("제주도"),
        "hybrid",
        [],
        planner_intent="visual",
        tag_vocab=TagVocabulary(
            place_tags=frozenset({"제주도", "제주"}),
            all_tags=frozenset({"제주도", "제주"}),
        ),
    )

    assert mode == "semantic"
    assert reason == "auto-travel"


def test_shadow_backend_skips_generic_face_hint_for_named_person_query(monkeypatch) -> None:
    backend = object.__new__(SqlAlchemyHybridSearchBackend)
    tagged = [{"file_id": "named-face", "tags": [{"type": "person", "value": "박지호"}]}]

    monkeypatch.setattr(backend, "_tagged_shadow_results", lambda query, *, limit, extra_terms=None, plan=None: tagged)
    monkeypatch.setattr(backend, "_resolve_person_tag_ids", lambda query: {"person-000001"})
    monkeypatch.setattr(
        backend,
        "_hinted_shadow_results",
        lambda query, *, limit, exclude_file_ids=None: [{"file_id": "generic-face"}],
    )
    monkeypatch.setattr(backend, "_filter_results", lambda results, plan, *, limit: results[:limit])

    results = SqlAlchemyHybridSearchBackend.search_by_shadow_doc(
        backend,
        "박지호",
        limit=10,
        plan=QueryPlan(
            original_query="박지호",
            normalized_query="박지호",
            keyword_query="박지호",
            visual_queries=["박지호"],
            date_from=None,
            date_to=None,
            person_terms=["박지호"],
            place_terms=[],
            ocr_terms=[],
            visual_terms=[],
            intent="visual",
        ),
    )

    assert results == tagged


def test_pure_person_alias_search_skips_ocr_extras() -> None:
    class PersonOnlyBackend(FakeBackend):
        def get_tag_vocabulary(self) -> TagVocabulary:
            return TagVocabulary(
                person_tags=frozenset({"와이프"}),
                all_tags=frozenset({"와이프"}),
            )

        def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
            return [{"file_id": "ocr-extra", "ocr_text": "와이프", "ocr_match_kind": "word"}]

        def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
            return [
                {
                    "file_id": "person-photo",
                    "tags": [{"type": "person", "value": "와이프"}],
                    "tag_exact_match": True,
                }
            ]

    service = HybridSearchService(PersonOnlyBackend())

    results, meta = service.search_with_meta("와이프", limit=10, mode="hybrid", debug=True)

    assert meta["debug"]["channel_stats"]["ocr"] == 0
    assert [item["file_id"] for item in results] == ["person-photo"]


def test_semantic_query_uses_auto_tags_as_ranking_signal() -> None:
    service = HybridSearchService(FakeBackend())

    results, meta = service.search_with_meta("baby", limit=5, mode="hybrid")

    assert meta["effective_mode"] == "semantic"
    assert results[0]["file_id"] == "auto-baby"
    assert "태그 일치" in results[0]["match_explanation"]


def test_single_named_person_search_prefers_solo_photos_over_group_photos() -> None:
    plan = plan_query(
        "서연",
        tag_vocab=TagVocabulary(
            person_tags=frozenset({"서연", "서연이"}),
            all_tags=frozenset({"서연", "서연이"}),
        ),
    )
    results = [
        {
            "file_id": "group",
            "tag_exact_match": True,
            "rank_score": 1.0,
            "person_count": 2,
            "tags": [{"type": "person", "value": "서연"}],
        },
        {
            "file_id": "solo",
            "tag_exact_match": True,
            "rank_score": 0.2,
            "person_count": 1,
            "tags": [{"type": "person", "value": "서연"}],
        },
    ]

    apply_single_person_solo_priority(results, plan)
    results.sort(key=search_sort_key, reverse=True)

    assert results[0]["file_id"] == "solo"
    assert results[0]["person_solo_match"] is True
    assert results[1]["person_group_match"] is True


def test_named_person_search_skips_burst_and_daily_caps() -> None:
    plan = plan_query(
        "박지호",
        tag_vocab=TagVocabulary(
            person_tags=frozenset({"박지호"}),
            all_tags=frozenset({"박지호"}),
        ),
    )
    results = [
        {
            "file_id": "a",
            "rank_score": 1.0,
            "captured_at": "2026-05-01T10:00:00",
            "tags": [{"type": "person", "value": "박지호"}],
        },
        {
            "file_id": "b",
            "rank_score": 0.9,
            "captured_at": "2026-05-01T10:00:01",
            "tags": [{"type": "person", "value": "박지호"}],
        },
        {
            "file_id": "c",
            "rank_score": 0.8,
            "captured_at": "2026-05-01T12:00:00",
            "tags": [{"type": "person", "value": "박지호"}],
        },
        {
            "file_id": "d",
            "rank_score": 0.7,
            "captured_at": "2026-05-01T13:00:00",
            "tags": [{"type": "person", "value": "박지호"}],
        },
        {
            "file_id": "e",
            "rank_score": 0.6,
            "captured_at": "2026-05-01T14:00:00",
            "tags": [{"type": "person", "value": "박지호"}],
        },
        {
            "file_id": "f",
            "rank_score": 0.5,
            "captured_at": "2026-05-01T15:00:00",
            "tags": [{"type": "person", "value": "박지호"}],
        },
    ]

    named_person_focused = True
    if not named_person_focused:
        results = remove_near_duplicates(results)
    results = apply_diversity_cap(results, max_per_day=99999 if named_person_focused else 5)

    assert [item["file_id"] for item in results] == ["a", "b", "c", "d", "e", "f"]


def test_exact_tag_search_skips_burst_and_daily_caps() -> None:
    class ExactTagBackend(FakeBackend):
        def get_tag_vocabulary(self) -> TagVocabulary:
            return TagVocabulary(all_tags=frozenset({"party"}))

        def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
            return [
                {
                    "file_id": chr(ord("a") + index),
                    "rank_score": 1.0 - index * 0.01,
                    "captured_at": f"2026-05-01T10:00:0{index}",
                    "tag_exact_match": True,
                    "tags": [{"type": "auto_event", "value": "party"}],
                }
                for index in range(6)
            ]

        def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
            return []

    service = HybridSearchService(ExactTagBackend())

    results, _meta = service.search_with_meta("party", limit=10, mode="hybrid")

    assert [item["file_id"] for item in results] == ["a", "b", "c", "d", "e", "f"]


def test_exact_tag_search_skips_ocr_extras() -> None:
    class ExactTagWithOcrBackend(FakeBackend):
        def get_tag_vocabulary(self) -> TagVocabulary:
            return TagVocabulary(all_tags=frozenset({"춘천시"}))

        def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
            return [{"file_id": "ocr-extra", "ocr_text": "춘천시", "ocr_match_kind": "word"}]

        def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
            return [
                {
                    "file_id": "exact-place",
                    "tags": [{"type": "place", "value": "춘천시"}],
                    "tag_exact_match": True,
                }
            ]

        def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
            return []

    service = HybridSearchService(ExactTagWithOcrBackend())

    results, meta = service.search_with_meta("춘천시", limit=10, mode="ocr", debug=True)

    assert meta["debug"]["channel_stats"]["ocr"] == 0
    assert [item["file_id"] for item in results] == ["exact-place"]


def test_custom_reranker_order_is_preserved() -> None:
    service = HybridSearchService(FakeBackend(), reranker=ReverseReranker())

    results, meta = service.search_with_meta("random visual query", limit=5, mode="hybrid")

    assert meta["effective_mode"] == "hybrid"
    assert [item["file_id"] for item in results[:2]] == ["file-b", "file-a"]


def test_golden_visual_query_uses_clip_and_shadow_channels() -> None:
    service = HybridSearchService(GoldenChannelBackend())

    results, meta = service.search_with_meta("바다 여행", limit=5, mode="hybrid", debug=True)

    assert meta["effective_mode"] == "semantic"
    assert meta["intent_reason"] == "auto-travel"
    assert meta["query_plan"]["intent"] == "visual"
    assert meta["debug"]["channel_stats"] == {
        "ocr": 0,
        "clip": 1,
        "shadow": 1,
        "fused": 1,
        "final": 1,
    }
    assert results[0]["file_id"] == "beach-trip"
    assert results[0]["match_reason"] == "clip+shadow"


def test_clear_visual_query_skips_ocr_channel() -> None:
    class CountingVisualBackend(GoldenChannelBackend):
        def __init__(self) -> None:
            self.ocr_calls = 0

        def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
            self.ocr_calls += 1
            return []

    backend = CountingVisualBackend()
    service = HybridSearchService(backend)

    results, meta = service.search_with_meta("바다 여행", limit=5, mode="hybrid", debug=True)

    assert backend.ocr_calls == 0
    assert meta["effective_mode"] == "semantic"
    assert meta["debug"]["channel_stats"]["ocr"] == 0
    assert results[0]["file_id"] == "beach-trip"


def test_golden_ocr_query_skips_clip_channel() -> None:
    service = HybridSearchService(GoldenChannelBackend())

    results, meta = service.search_with_meta("영수증 오류", limit=5, mode="hybrid", debug=True)

    assert meta["effective_mode"] == "ocr"
    assert meta["query_plan"]["intent"] == "ocr"
    assert meta["debug"]["channel_stats"]["ocr"] == 1
    assert meta["debug"]["channel_stats"]["clip"] == 0
    assert meta["debug"]["channel_stats"]["shadow"] == 1
    assert results[0]["file_id"] == "receipt-file"


def test_condition_fallback_relaxes_to_place_term() -> None:
    """When a compound query finds nothing, fallback tries each place term alone."""

    class NoResultBackend(FakeBackend):
        def search_by_ocr(self, query: str, *, limit: int) -> list[dict]:
            return []

        def search_by_embedding(self, query_embedding: bytes, **_kwargs) -> list[dict]:
            return []

        def search_by_shadow_doc(self, query: str, *, limit: int) -> list[dict]:
            # Only returns results when queried with a single known place term
            if query in ("바다", "sea", "beach"):
                return [{"file_id": "beach-file", "distance": 0.3}]
            return []

    service = HybridSearchService(NoResultBackend())

    # Complex query with place+person+date that finds nothing combined
    results, meta = service.search_with_meta(
        "작년 여름 바다에서 가족이랑 찍은 사진", limit=5, mode="hybrid"
    )

    assert results, "expected condition fallback to find results"
    assert meta.get("fallback") in (
        "condition_visual_only", "condition_place_only", "date_relaxed"
    )


def test_place_hard_filter_accepts_hierarchical_geocode_tag() -> None:
    plan = QueryPlan(
        original_query="스위스에서 찍은 사진",
        normalized_query="스위스에서 찍은 사진",
        keyword_query="스위스",
        visual_queries=["스위스에서 찍은 사진"],
        date_from=None,
        date_to=None,
        person_terms=[],
        place_terms=["스위스"],
        ocr_terms=[],
        visual_terms=[],
        intent="visual",
        require_place_match=True,
    )
    results = [
        {"file_id": "zurich", "tags": [{"type": "place", "value": "스위스 취리히"}]},
        {"file_id": "tokyo", "tags": [{"type": "place", "value": "일본 도쿄"}]},
    ]

    filtered = apply_hard_filters(results, plan)

    assert [item["file_id"] for item in filtered] == ["zurich"]


def test_compound_person_place_query_requires_both_terms() -> None:
    plan = QueryPlan(
        original_query="서연이랑 바다에서 찍은 사진",
        normalized_query="서연이랑 바다에서 찍은 사진",
        keyword_query="서연 바다",
        visual_queries=["서연이랑 바다에서 찍은 사진"],
        date_from=None,
        date_to=None,
        person_terms=["서연", "서연이"],
        place_terms=["바다"],
        ocr_terms=[],
        visual_terms=[],
        intent="visual",
        require_place_match=True,
    )
    results = [
        {"file_id": "both", "tags": [{"type": "person", "value": "서연"}, {"type": "place", "value": "바다"}]},
        {"file_id": "person-only", "tags": [{"type": "person", "value": "서연"}]},
        {"file_id": "place-only", "tags": [{"type": "place", "value": "바다"}]},
    ]

    filtered = apply_hard_filters(results, plan)

    assert [item["file_id"] for item in filtered] == ["both"]


def test_compound_place_visual_query_requires_both_terms() -> None:
    plan = QueryPlan(
        original_query="스위스에서 찍은 자전거 사진",
        normalized_query="스위스에서 찍은 자전거 사진",
        keyword_query="스위스 자전거",
        visual_queries=["스위스에서 찍은 자전거 사진"],
        date_from=None,
        date_to=None,
        person_terms=[],
        place_terms=["스위스"],
        ocr_terms=[],
        visual_terms=["자전거"],
        intent="visual",
        require_place_match=True,
        require_visual_match=True,
    )
    results = [
        {"file_id": "both", "tags": [{"type": "place", "value": "스위스 취리히"}, {"type": "auto_object", "value": "bicycle"}]},
        {"file_id": "place-only", "tags": [{"type": "place", "value": "스위스 취리히"}]},
        {"file_id": "visual-only", "tags": [{"type": "auto_object", "value": "bicycle"}]},
    ]

    filtered = apply_hard_filters(results, plan)

    assert [item["file_id"] for item in filtered] == ["both"]


def test_dynamic_visual_tags_are_planned_as_required_and_condition() -> None:
    vocab = TagVocabulary(
        place_tags=frozenset({"日本"}),
        visual_tags=frozenset({"cloud"}),
        all_tags=frozenset({"日本", "cloud"}),
    )
    plan = plan_query("日本에서 찍은 cloud 사진", tag_vocab=vocab)

    assert "日本".casefold() in plan.place_terms
    assert "cloud" in plan.visual_terms
    assert plan.require_place_match is True
    assert plan.require_visual_match is True


def test_dynamic_latin_place_reverse_match_does_not_add_sibling_place() -> None:
    vocab = TagVocabulary(
        place_tags=frozenset({"nusa tenggara timur", "jawa timur"}),
        all_tags=frozenset({"nusa tenggara timur", "jawa timur"}),
    )
    plan = plan_query("Nusa Tenggara Timur에서 찍은 사진", tag_vocab=vocab)

    assert "nusa tenggara timur" in plan.place_terms
    assert "jawa timur" not in plan.place_terms


def test_heuristic_splits_inner_joiner_compound() -> None:
    """가족이랑바다여행 should tokenize to [가족, 바다, 여행] without KoNLPy."""
    tokens = korean_nouns("가족이랑바다여행")
    assert "가족" in tokens
    assert "바다" in tokens
    assert "여행" in tokens


def test_heuristic_splits_particle_attached_token() -> None:
    """바다에서 (4 chars) should be split to [바다] with the lowered threshold."""
    tokens = korean_nouns("바다에서")
    assert "바다" in tokens


def test_heuristic_splits_rang_joiner() -> None:
    """엄마랑카페 should split to [엄마, 카페]."""
    tokens = korean_nouns("엄마랑카페")
    assert "엄마" in tokens
    assert "카페" in tokens


class _FeedbackBackend:
    """Minimal backend exposing only load_query_feedback for the reranker."""

    def __init__(self, pinned: set[str], corrections: dict[str, str]) -> None:
        self._pinned = pinned
        self._corrections = corrections

    def load_query_feedback(self, query: str) -> tuple[set[str], dict[str, str]]:
        return set(self._pinned), dict(self._corrections)


def test_feedback_reranker_pins_promote_and_matching_tag_correction() -> None:
    backend = _FeedbackBackend(
        pinned={"f-promoted"},
        corrections={"f-sea": "바다", "f-mtn": "산"},
    )
    results = [
        {"file_id": "f-a", "rank_score": 0.9},
        {"file_id": "f-promoted", "rank_score": 0.3},
        {"file_id": "f-sea", "rank_score": 0.2},
        {"file_id": "f-mtn", "rank_score": 0.8},
    ]
    out = FeedbackReranker(backend).rerank(results, plan_query("바다 사진"))
    ids = [r["file_id"] for r in out]

    # query-scoped promote + tag-correction matching the query ('바다') are pinned,
    # preserving their original relative order
    assert ids[:2] == ["f-promoted", "f-sea"]
    # '산' correction does not appear in the query → f-mtn stays in the tail
    assert "f-mtn" in ids[2:]
    # nothing is dropped
    assert set(ids) == {"f-a", "f-promoted", "f-sea", "f-mtn"}
    # pin leaves an audit trail
    pinned_sea = next(r for r in out if r["file_id"] == "f-sea")
    assert any(b["stage"] == "feedback_pin" for b in pinned_sea["score_breakdown"])


def test_feedback_reranker_is_noop_without_matching_feedback() -> None:
    backend = _FeedbackBackend(pinned=set(), corrections={"f-x": "고양이"})
    results = [
        {"file_id": "f-a", "rank_score": 0.9},
        {"file_id": "f-x", "rank_score": 0.1},
    ]
    out = FeedbackReranker(backend).rerank(results, plan_query("바다"))
    assert [r["file_id"] for r in out] == ["f-a", "f-x"]  # unchanged order


def test_feedback_reranker_tolerates_backend_without_support() -> None:
    class _Bare:
        pass

    results = [{"file_id": "f-a", "rank_score": 0.5}]
    out = FeedbackReranker(_Bare()).rerank(results, plan_query("바다"))
    assert out == results


def test_plan_query_requires_all_persons_for_multiple_names() -> None:
    vocab = TagVocabulary(
        person_tags=frozenset({"박지호", "이서연"}),
        all_tags=frozenset({"박지호", "이서연"}),
    )
    plan = plan_query("박지호이랑 이서연이랑 찍은 사진", tag_vocab=vocab)
    assert set(plan.person_terms) == {"박지호", "이서연"}
    assert plan.require_all_persons is True
    assert plan.requires_person_match() is True

    # 명시적 OR 표현이 있으면 합집합(OR)로 둔다
    plan_or = plan_query("박지호 또는 이서연", tag_vocab=vocab)
    assert set(plan_or.person_terms) == {"박지호", "이서연"}
    assert plan_or.require_all_persons is False


def test_matches_person_terms_and_requires_every_named_person() -> None:
    from app.services.search.hybrid import _matches_person_terms
    from app.services.search.planner import QueryPlan

    plan = QueryPlan(
        original_query="박지호이랑 이서연이랑 찍은 사진",
        normalized_query="박지호 이서연 찍은 사진",
        keyword_query="박지호 이서연",
        visual_queries=[], date_from=None, date_to=None,
        person_terms=["박지호", "이서연"], place_terms=[], ocr_terms=[],
        visual_terms=[], intent="visual", require_all_persons=True,
    )
    both = {"tags": [{"type": "person", "value": "박지호"},
                     {"type": "person", "value": "이서연"}]}
    only_one = {"tags": [{"type": "person", "value": "박지호"}],
                "matched_person_ids": [40]}
    # 두 사람 모두 있는 사진만 통과(AND), 한 사람만 있으면 matched id가 있어도 제외
    assert _matches_person_terms(both, plan) is True
    assert _matches_person_terms(only_one, plan) is False
