from datetime import UTC, datetime

import pytest

from scripts.evaluate_product_search import SearchObservation, build_summary
from scripts.product_search_dataset import EvaluationCase, EvaluationDataset


def _dataset() -> EvaluationDataset:
    return EvaluationDataset(
        dataset_version="1.0.0",
        dataset_kind="development",
        sampling_seed=1,
        generated_at=datetime.now(UTC),
        cases=[
            EvaluationCase(
                case_id="DEV-001",
                query="제품 A",
                scenario="full_product_name",
                product_category="스킨케어",
                source_product_id=1,
                source_product_name="제품 A",
                source_brand="브랜드",
                acceptable_product_ids=[1, 2],
                expected_result="found",
                review_note="검수",
            ),
            EvaluationCase(
                case_id="DEV-002",
                query="제품 B",
                scenario="core_product_name",
                product_category="클렌징",
                source_product_id=3,
                source_product_name="제품 B",
                source_brand="브랜드",
                acceptable_product_ids=[3],
                expected_result="found",
                review_note="검수",
            ),
            EvaluationCase(
                case_id="NR-001",
                query="없는 제품",
                scenario="not_registered",
                product_category=None,
                source_product_id=None,
                source_product_name=None,
                source_brand=None,
                acceptable_product_ids=[],
                expected_result="empty",
                review_note="검수",
            ),
        ],
    )


def test_build_summary_reports_quality_latency_and_consistency() -> None:
    observations = [
        SearchObservation("DEV-001", repeat, [9, 1, 8], 10.0 + repeat) for repeat in range(1, 6)
    ]
    observations += [
        SearchObservation("DEV-002", repeat, [], 20.0 + repeat) for repeat in range(1, 6)
    ]
    observations += [
        SearchObservation("NR-001", repeat, [], 30.0 + repeat) for repeat in range(1, 6)
    ]

    summary = build_summary(_dataset(), observations)

    assert summary["registered"]["hit_at_1"] == 0.0
    assert summary["registered"]["hit_at_5"] == 0.5
    assert summary["registered"]["hit_at_10"] == 0.5
    assert summary["registered"]["mrr_at_5"] == 0.25
    assert summary["not_registered"]["accuracy"] == 1.0
    assert summary["request_success_rate"] == 1.0
    assert summary["top5_consistency_rate"] == 1.0
    assert summary["latency_ms"]["p90"] == pytest.approx(33.6)
    assert summary["latency_ms"]["p95"] == pytest.approx(34.3)
    assert summary["latency_ms"]["p99"] == pytest.approx(34.86)
    assert summary["by_scenario"]["full_product_name"]["hit_at_5"] == 1.0
    assert summary["by_category"]["클렌징"]["hit_at_5"] == 0.0


def test_build_summary_records_failures_and_unstable_results() -> None:
    observations = [
        SearchObservation("DEV-001", 1, [1], 10.0),
        SearchObservation("DEV-001", 2, [2], 11.0),
        SearchObservation("DEV-001", 3, [], 2_000.0, "timeout"),
        SearchObservation("DEV-001", 4, [1], 12.0),
        SearchObservation("DEV-001", 5, [1], 13.0),
    ]

    summary = build_summary(
        EvaluationDataset(
            **_dataset().model_dump(exclude={"cases"}),
            cases=[_dataset().cases[0]],
        ),
        observations,
    )

    assert summary["request_success_rate"] == 0.8
    assert summary["error_count"] == 1
    assert summary["top5_consistency_rate"] == 0.0
    assert summary["repeated_failure_case_ids"] == []
    assert summary["latency_ms"]["sample_count"] == 4
