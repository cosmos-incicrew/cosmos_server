from scripts.evaluate_ingredient_search import (
    SearchObservation,
    _fingerprint_digest,
    build_summary,
)
from scripts.ingredient_search_dataset import (
    IngredientEvaluationCase,
    IngredientEvaluationDataset,
)


def _case(
    case_id: str,
    ingredient_id: int | None,
    expected_result: str = "found",
) -> IngredientEvaluationCase:
    return IngredientEvaluationCase(
        case_id=case_id,
        query=f"검색어{case_id}",
        scenario="exact_standard_name" if expected_result == "found" else "not_registered",
        source_ingredient_id=ingredient_id,
        source_standard_name="성분" if ingredient_id else None,
        source_matched_name="성분" if ingredient_id else None,
        acceptable_ingredient_ids=[ingredient_id] if ingredient_id else [],
        expected_result=expected_result,
        review_status="approved",
    )


def test_build_summary_calculates_quality_latency_and_consistency() -> None:
    dataset = IngredientEvaluationDataset(
        dataset_version="1.0.0",
        dataset_kind="final",
        sampling_seed=1,
        generated_at="2026-07-22T00:00:00Z",
        cases=[_case("IS-001", 10), _case("IS-002", None, "empty")],
    )
    observations = [
        SearchObservation("IS-001", 1, [20, 10], 10.0),
        SearchObservation("IS-002", 1, [], 20.0),
        SearchObservation("IS-001", 2, [20, 10], 30.0),
        SearchObservation("IS-002", 2, [], 40.0),
    ]

    summary = build_summary(dataset, observations)

    assert summary["registered"]["hit_at_1"] == 0.0
    assert summary["registered"]["hit_at_5"] == 1.0
    assert summary["registered"]["mrr_at_5"] == 0.5
    assert summary["not_registered"]["accuracy"] == 1.0
    assert summary["latency_ms"]["p95"] == 38.5
    assert summary["top5_consistency_rate"] == 1.0


def test_fingerprint_digest_is_stable_for_dictionary_key_order() -> None:
    left = [{"ingredient_id": 1, "name_kor": "정제수"}]
    right = [{"name_kor": "정제수", "ingredient_id": 1}]

    assert _fingerprint_digest(left) == _fingerprint_digest(right)
