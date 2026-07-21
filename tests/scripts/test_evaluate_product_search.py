import pytest

from scripts.evaluate_product_search import (
    ProductRow,
    SearchCase,
    SearchObservation,
    _build_cases,
    build_summary,
    partial_query_from_product_name,
)


def test_build_summary_reports_quality_reliability_and_latency() -> None:
    observations = [
        SearchObservation(
            case=SearchCase("exact", "제품 A", 1),
            result_ids=[2, 1],
            latency_ms=10.0,
        ),
        SearchObservation(
            case=SearchCase("exact", "제품 B", 3),
            result_ids=[],
            latency_ms=20.0,
        ),
        SearchObservation(
            case=SearchCase("partial", "제품", 4),
            result_ids=[4, 5],
            latency_ms=30.0,
        ),
        SearchObservation(
            case=SearchCase("no_result", "없는 제품", None),
            result_ids=[],
            latency_ms=40.0,
        ),
        SearchObservation(
            case=SearchCase("no_result", "역시 없는 제품", None),
            result_ids=[9],
            latency_ms=50.0,
        ),
        SearchObservation(
            case=SearchCase("partial", "오류", 10),
            result_ids=[],
            latency_ms=60.0,
            error="temporary failure",
        ),
    ]

    summary = build_summary(observations, result_limit=20)

    assert summary == {
        "total_queries": 6,
        "successful_queries": 5,
        "error_count": 1,
        "success_rate": pytest.approx(5 / 6),
        "latency_ms": {"p50": 30.0, "p95": 48.0, "max": 50.0},
        "exact": {
            "query_count": 2,
            "hit_at_20": 0.5,
            "mrr_at_20": 0.25,
        },
        "partial": {
            "query_count": 1,
            "non_empty_rate": 1.0,
            "sample_target_hit_at_20": 1.0,
            "sample_target_mrr_at_20": 1.0,
        },
        "no_result": {
            "query_count": 2,
            "accuracy": 0.5,
        },
    }


@pytest.mark.parametrize(
    ("product_name", "expected"),
    [
        ("[NEW/단독] 바이오던스 리포좀 버블 부스터", "바이오던"),
        ("에스트라 아토베리어365 크림 80ml", "에스트라"),
        ("  1,2-헥산다이올 세럼", "헥산다이"),
    ],
)
def test_partial_query_uses_a_readable_product_name_prefix(
    product_name: str, expected: str
) -> None:
    assert partial_query_from_product_name(product_name) == expected


def test_build_cases_keeps_counts_uniqueness_and_full_range_sampling() -> None:
    products = [
        ProductRow(
            id=index,
            product_name=f"P{index:03d} 수분 크림",
            main_category=f"category-{index % 4}",
        )
        for index in range(1, 121)
    ]

    cases = _build_cases(
        products,
        exact_count=40,
        partial_count=40,
        no_result_count=20,
        seed=20260721,
    )

    assert [case.kind for case in cases].count("exact") == 40
    assert [case.kind for case in cases].count("partial") == 40
    assert [case.kind for case in cases].count("no_result") == 20
    assert len({case.query for case in cases}) == 100
    selected_ids = [
        case.expected_product_id for case in cases if case.expected_product_id is not None
    ]
    assert min(selected_ids) < 10
    assert max(selected_ids) > 110
    assert all("cosmos-no-result" not in case.query for case in cases)
