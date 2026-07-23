import json
from pathlib import Path

import pytest

from scripts.ingredient_search_dataset import build_dataset, load_dataset


def _ingredients(count: int = 120) -> list[dict[str, object]]:
    return [
        {
            "ingredient_id": index,
            "name_kor": f"테스트표준성분{index}추출물",
            "name_eng": f"Test Ingredient {index} Extract",
        }
        for index in range(1, count + 1)
    ]


def _synonyms(count: int = 120) -> list[dict[str, object]]:
    return [
        {
            "synonym_id": index,
            "ingredient_id": index,
            "synonym": (
                f"AliasIngredient{index}Extract"
                if index % 2
                else f"별칭성분{index}추출물"
            ),
            "language": "eng" if index % 2 else "kor",
        }
        for index in range(1, count + 1)
    ]


def test_build_dataset_creates_unique_balanced_cases() -> None:
    dataset = build_dataset(_ingredients(), _synonyms(), seed=7)

    assert len(dataset.cases) == 100
    assert len({case.case_id for case in dataset.cases}) == 100
    registered = [case for case in dataset.cases if case.expected_result == "found"]
    assert len({case.source_ingredient_id for case in registered}) == 90
    assert all(case.source_ingredient_id in case.acceptable_ingredient_ids for case in registered)
    assert {scenario: sum(case.scenario == scenario for case in dataset.cases) for scenario in {
        "exact_standard_name", "partial_standard_name", "exact_synonym", "partial_synonym",
        "not_registered",
    }} == {
        "exact_standard_name": 25,
        "partial_standard_name": 25,
        "exact_synonym": 20,
        "partial_synonym": 20,
        "not_registered": 10,
    }


def test_build_dataset_excludes_previous_source_ingredients() -> None:
    excluded_ids = set(range(1, 31))

    dataset = build_dataset(
        _ingredients(180),
        _synonyms(180),
        seed=7,
        excluded_ingredient_ids=excluded_ids,
    )

    source_ids = {
        case.source_ingredient_id
        for case in dataset.cases
        if case.source_ingredient_id is not None
    }
    assert source_ids.isdisjoint(excluded_ids)


def test_load_dataset_rejects_duplicate_registered_ingredients(tmp_path: Path) -> None:
    case = {
        "case_id": "IS-001",
        "query": "테스트성분",
        "scenario": "exact_standard_name",
        "source_ingredient_id": 1,
        "source_standard_name": "테스트성분",
        "source_matched_name": "테스트성분",
        "acceptable_ingredient_ids": [1],
        "expected_result": "found",
    }
    payload = {
        "dataset_version": "0.1.0",
        "dataset_kind": "draft",
        "sampling_seed": 1,
        "generated_at": "2026-07-22T00:00:00Z",
        "cases": [case, {**case, "case_id": "IS-002"}],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="source_ingredient_id"):
        load_dataset(path)
