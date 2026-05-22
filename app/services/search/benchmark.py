"""Synthetic benchmark queries for tuning the Phase 2 search stack."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.search.hybrid import HybridSearchService


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    query: str
    expected_effective_mode: str | None = None
    expected_intent: str | None = None
    expected_person_terms: tuple[str, ...] = ()
    expected_place_terms: tuple[str, ...] = ()
    expected_ocr_terms: tuple[str, ...] = ()
    expected_visual_terms: tuple[str, ...] = ()
    expects_date_range: bool = False


DEFAULT_BENCHMARK_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="family_beach_last_summer",
        query="작년 여름 바다에서 가족이랑 찍은 사진",
        expected_effective_mode="semantic",
        expected_intent="visual",
        expected_person_terms=("가족",),
        expected_place_terms=("바다",),
        expected_visual_terms=("바다",),
        expects_date_range=True,
    ),
    BenchmarkCase(
        name="receipt_keyword",
        query="영수증",
        expected_effective_mode="ocr",
        expected_intent="ocr",
        expected_ocr_terms=("영수증",),
    ),
    BenchmarkCase(
        name="chat_error_screen",
        query="지난달 카톡 오류 캡처",
        expected_effective_mode="ocr",
        expected_intent="mixed",
        expected_ocr_terms=("오류", "카톡"),
        expects_date_range=True,
    ),
    BenchmarkCase(
        name="mother_cafe",
        query="엄마랑 카페",
        expected_effective_mode="semantic",
        expected_intent="visual",
        expected_person_terms=("엄마",),
        expected_place_terms=("카페",),
    ),
    BenchmarkCase(
        name="birthday_cake",
        query="생일 케이크",
        expected_effective_mode="semantic",
        expected_intent="visual",
        expected_visual_terms=("생일",),
    ),
    BenchmarkCase(
        name="baby_photo",
        query="아기 사진",
        expected_effective_mode="semantic",
        expected_intent="visual",
        expected_person_terms=("아기",),
        expected_visual_terms=("아기",),
    ),
)


def run_benchmark_suite(
    service: HybridSearchService,
    *,
    limit: int = 10,
    weight_overrides: dict[str, float] | None = None,
) -> dict:
    cases = [
        _run_case(service, case, limit=limit, weight_overrides=weight_overrides)
        for case in DEFAULT_BENCHMARK_CASES
    ]
    passed = sum(1 for case in cases if case["passed"])
    return {
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "weight_overrides": weight_overrides or {},
        "summary": _summary(cases),
        "cases": cases,
    }


def _run_case(
    service: HybridSearchService,
    case: BenchmarkCase,
    *,
    limit: int,
    weight_overrides: dict[str, float] | None = None,
) -> dict:
    items, meta = service.search_with_meta(
        case.query,
        limit=limit,
        mode="hybrid",
        debug=True,
        weight_overrides=weight_overrides,
    )
    query_plan = meta.get("query_plan", {})
    checks = []

    if case.expected_effective_mode is not None:
        checks.append(
            _check(
                "effective_mode",
                actual=meta.get("effective_mode"),
                expected=case.expected_effective_mode,
                passed=meta.get("effective_mode") == case.expected_effective_mode,
            )
        )
    if case.expected_intent is not None:
        checks.append(
            _check(
                "intent",
                actual=query_plan.get("intent"),
                expected=case.expected_intent,
                passed=query_plan.get("intent") == case.expected_intent,
            )
        )
    if case.expects_date_range:
        has_date_range = bool(query_plan.get("date_from") and query_plan.get("date_to"))
        checks.append(_check("date_range", actual=has_date_range, expected=True, passed=has_date_range))

    checks.extend(_term_checks("person_terms", query_plan.get("person_terms", []), case.expected_person_terms))
    checks.extend(_term_checks("place_terms", query_plan.get("place_terms", []), case.expected_place_terms))
    checks.extend(_term_checks("ocr_terms", query_plan.get("ocr_terms", []), case.expected_ocr_terms))
    checks.extend(_term_checks("visual_terms", query_plan.get("visual_terms", []), case.expected_visual_terms))

    return {
        "name": case.name,
        "query": case.query,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "meta": meta,
        "top_results": [
            {
                "file_id": item.get("file_id"),
                "filename": item.get("filename"),
                "match_reason": item.get("match_reason"),
                "match_explanation": item.get("match_explanation"),
                "rank_score": item.get("rank_score"),
            }
            for item in items[:5]
        ],
    }


def _term_checks(name: str, actual: list[str], expected: tuple[str, ...]) -> list[dict]:
    actual_set = set(actual)
    checks = []
    for term in expected:
        checks.append(
            _check(
                f"{name}:{term}",
                actual=term in actual_set,
                expected=True,
                passed=term in actual_set,
            )
        )
    return checks


def _check(name: str, *, actual, expected, passed: bool) -> dict:
    return {"name": name, "actual": actual, "expected": expected, "passed": passed}


def _summary(cases: list[dict]) -> dict:
    failed_checks: dict[str, int] = {}
    effective_modes: dict[str, int] = {}
    intents: dict[str, int] = {}
    for case in cases:
        meta = case.get("meta", {})
        query_plan = meta.get("query_plan", {})
        effective_mode = str(meta.get("effective_mode") or "unknown")
        intent = str(query_plan.get("intent") or "unknown")
        effective_modes[effective_mode] = effective_modes.get(effective_mode, 0) + 1
        intents[intent] = intents.get(intent, 0) + 1
        for check in case.get("checks", []):
            if not check.get("passed"):
                name = str(check.get("name") or "unknown")
                failed_checks[name] = failed_checks.get(name, 0) + 1
    return {
        "effective_modes": effective_modes,
        "intents": intents,
        "failed_checks": failed_checks,
    }
