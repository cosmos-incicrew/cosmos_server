"""실제 Supabase 데이터로 성분명 검색 평가 데이터셋 초안을 생성한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.supabase import create_supabase_client
from app.modules.ingredient_search.ingredient_matching import normalize_ingredient_text

DEFAULT_OUTPUT = Path("evaluation/ingredient_search/datasets/final-candidate-v0.1.0.json")
DEFAULT_SEED = 20260722
DATASET_VERSION = "0.1.0"
DEFAULT_DATASET_KIND = "final_candidate"
EXACT_STANDARD_SCENARIO = "exact_standard_name"
PARTIAL_STANDARD_SCENARIO = "partial_standard_name"
EXACT_SYNONYM_SCENARIO = "exact_synonym"
PARTIAL_SYNONYM_SCENARIO = "partial_synonym"
NOT_REGISTERED_SCENARIO = "not_registered"
EXPECTED_FOUND = "found"
EXPECTED_EMPTY = "empty"
STANDARD_CASE_COUNTS = {"kor": 15, "eng": 10}
SYNONYM_CASE_COUNTS = {"kor": 10, "eng": 10}
NOT_REGISTERED_CASE_COUNT = 10
REGISTERED_CASE_COUNT = 2 * (
    sum(STANDARD_CASE_COUNTS.values()) + sum(SYNONYM_CASE_COUNTS.values())
)
MIN_SEARCH_QUERY_LENGTH = 2
MAX_SEARCH_QUERY_LENGTH = 100
MIN_PARTIAL_SOURCE_LENGTH = 4
MIN_PARTIAL_QUERY_LENGTH = 3
PARTIAL_QUERY_RATIO = 0.8
_PAGE_SIZE = 1_000


class DatasetSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str
    supabase_service_role_key: str


@dataclass(frozen=True)
class IngredientEvaluationCase:
    case_id: str
    query: str
    scenario: str
    source_ingredient_id: int | None
    source_standard_name: str | None
    source_matched_name: str | None
    acceptable_ingredient_ids: list[int]
    expected_result: str
    source_language: str | None = None
    review_status: str = "draft"
    review_note: str = "자동 생성 초안—사람 검수 필요"


@dataclass(frozen=True)
class IngredientEvaluationDataset:
    dataset_version: str
    dataset_kind: str
    sampling_seed: int
    generated_at: str
    cases: list[IngredientEvaluationCase]


def load_dataset(path: Path) -> IngredientEvaluationDataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [IngredientEvaluationCase(**case) for case in raw["cases"]]
    dataset = IngredientEvaluationDataset(
        dataset_version=str(raw["dataset_version"]),
        dataset_kind=str(raw["dataset_kind"]),
        sampling_seed=int(raw["sampling_seed"]),
        generated_at=str(raw["generated_at"]),
        cases=cases,
    )
    _validate_dataset(dataset)
    return dataset


def _validate_dataset(dataset: IngredientEvaluationDataset) -> None:
    case_ids = [case.case_id for case in dataset.cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("case_id가 중복되었습니다.")
    registered_ids = [
        case.source_ingredient_id
        for case in dataset.cases
        if case.expected_result == EXPECTED_FOUND
    ]
    if None in registered_ids or len(registered_ids) != len(set(registered_ids)):
        raise ValueError("등록 성분 케이스는 서로 다른 source_ingredient_id가 필요합니다.")
    for case in dataset.cases:
        if len(normalize_ingredient_text(case.query)) < MIN_SEARCH_QUERY_LENGTH:
            raise ValueError(f"검색어가 너무 짧습니다: {case.case_id}")
        if case.expected_result == EXPECTED_FOUND and not case.acceptable_ingredient_ids:
            raise ValueError(f"정답 성분 ID가 없습니다: {case.case_id}")
        if case.expected_result == EXPECTED_EMPTY and case.acceptable_ingredient_ids:
            raise ValueError(f"미등록 케이스에 정답 성분 ID가 있습니다: {case.case_id}")


async def _fetch_all(client: Any, table: str, columns: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = await (
            client.table(table)
            .select(columns)
            .order(columns.split(",", maxsplit=1)[0])
            .range(offset, offset + _PAGE_SIZE - 1)
            .execute()
        )
        batch = [row for row in (response.data or []) if isinstance(row, dict)]
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return rows
        offset += len(batch)


def _partial_query(value: str) -> str | None:
    normalized = normalize_ingredient_text(value)
    if len(normalized) < MIN_PARTIAL_SOURCE_LENGTH:
        return None
    # 지나치게 짧은 접두어는 사용자의 의도를 특정하지 못해 정답 ID가 임의적이 된다.
    # 전체 표기의 대부분을 유지하되 완전 일치와는 구분되는 입력을 만든다.
    length = max(
        MIN_PARTIAL_QUERY_LENGTH,
        min(len(normalized) - 1, math.ceil(len(normalized) * PARTIAL_QUERY_RATIO)),
    )
    if any(character.isdigit() for character in normalized[length:]):
        return None
    return normalized[:length]


def build_dataset(
    ingredients: list[dict[str, Any]],
    synonyms: list[dict[str, Any]],
    seed: int,
    excluded_ingredient_ids: set[int] | None = None,
    dataset_version: str = DATASET_VERSION,
    dataset_kind: str = DEFAULT_DATASET_KIND,
) -> IngredientEvaluationDataset:
    rng = random.Random(seed)
    excluded_ids = excluded_ingredient_ids or set()
    ingredient_rows = list(ingredients)
    ingredient_by_id = {
        ingredient_id: row
        for row in ingredient_rows
        if isinstance((ingredient_id := row.get("ingredient_id")), int)
        and ingredient_id not in excluded_ids
        and isinstance(row.get("name_kor"), str)
    }
    synonym_rows = [
        row
        for row in synonyms
        if isinstance(row.get("ingredient_id"), int)
        and row["ingredient_id"] in ingredient_by_id
        and isinstance(row.get("synonym"), str)
        and normalize_ingredient_text(row["synonym"])
    ]
    standard_ids_by_name: dict[str, set[int]] = defaultdict(set)
    for ingredient_id, row in ingredient_by_id.items():
        for column in ("name_kor", "name_eng"):
            if isinstance((name := row.get(column)), str):
                standard_ids_by_name[normalize_ingredient_text(name)].add(ingredient_id)
    synonym_ids_by_name: dict[str, set[int]] = defaultdict(set)
    for row in synonym_rows:
        synonym_ids_by_name[normalize_ingredient_text(row["synonym"])].add(row["ingredient_id"])
    searchable_names_by_id: dict[int, set[str]] = defaultdict(set)
    for name, ingredient_ids in standard_ids_by_name.items():
        for ingredient_id in ingredient_ids:
            searchable_names_by_id[ingredient_id].add(name)
    for name, ingredient_ids in synonym_ids_by_name.items():
        for ingredient_id in ingredient_ids:
            searchable_names_by_id[ingredient_id].add(name)
    rng.shuffle(ingredient_rows)
    rng.shuffle(synonym_rows)
    used_ids: set[int] = set()
    raw_cases: list[tuple[str, str, int, str, str, list[int], str]] = []

    def partial_acceptable_ids(query: str) -> list[int]:
        normalized_query = normalize_ingredient_text(query)
        return sorted(
            ingredient_id
            for ingredient_id, names in searchable_names_by_id.items()
            if any(normalized_query in name for name in names)
        )

    def add_standard(
        scenario: str,
        count: int,
        partial: bool,
        column: str,
        language: str,
    ) -> None:
        added = 0
        for row in ingredient_rows:
            ingredient_id = row.get("ingredient_id")
            name = row.get(column)
            if (
                not isinstance(ingredient_id, int)
                or ingredient_id not in ingredient_by_id
                or ingredient_id in used_ids
            ):
                continue
            query = _partial_query(name) if isinstance(name, str) and partial else name
            if not isinstance(name, str) or not query:
                continue
            if len(query) > MAX_SEARCH_QUERY_LENGTH:
                continue
            used_ids.add(ingredient_id)
            acceptable_ids = (
                sorted(standard_ids_by_name[normalize_ingredient_text(name)])
                if not partial
                else partial_acceptable_ids(query)
            )
            raw_cases.append(
                (scenario, query, ingredient_id, name, name, acceptable_ids, language)
            )
            added += 1
            if added == count:
                return
        raise ValueError(f"{scenario}/{language} 후보가 부족합니다.")

    def add_synonyms(scenario: str, count: int, partial: bool, language: str) -> None:
        added = 0
        for row in synonym_rows:
            ingredient_id = row["ingredient_id"]
            if ingredient_id in used_ids or row.get("language") != language:
                continue
            synonym = row["synonym"].strip()
            query = _partial_query(synonym) if partial else synonym
            if not query or len(query) > MAX_SEARCH_QUERY_LENGTH:
                continue
            standard_name = ingredient_by_id[ingredient_id]["name_kor"]
            if normalize_ingredient_text(synonym) == normalize_ingredient_text(standard_name):
                continue
            used_ids.add(ingredient_id)
            acceptable_ids = (
                sorted(
                    synonym_ids_by_name[normalize_ingredient_text(synonym)]
                    | standard_ids_by_name[normalize_ingredient_text(synonym)]
                )
                if not partial
                else partial_acceptable_ids(query)
            )
            raw_cases.append(
                (
                    scenario,
                    query,
                    ingredient_id,
                    standard_name,
                    synonym,
                    acceptable_ids,
                    language,
                )
            )
            added += 1
            if added == count:
                return
        raise ValueError(f"{scenario}/{language} 후보가 부족합니다.")

    for language, count in STANDARD_CASE_COUNTS.items():
        column = "name_kor" if language == "kor" else "name_eng"
        add_standard(EXACT_STANDARD_SCENARIO, count, False, column, language)
    for language, count in STANDARD_CASE_COUNTS.items():
        column = "name_kor" if language == "kor" else "name_eng"
        add_standard(PARTIAL_STANDARD_SCENARIO, count, True, column, language)
    for language, count in SYNONYM_CASE_COUNTS.items():
        add_synonyms(EXACT_SYNONYM_SCENARIO, count, False, language)
    for language, count in SYNONYM_CASE_COUNTS.items():
        add_synonyms(PARTIAL_SYNONYM_SCENARIO, count, True, language)

    cases = [
        IngredientEvaluationCase(
            case_id=f"IS-{index:03d}",
            query=query,
            scenario=scenario,
            source_ingredient_id=ingredient_id,
            source_standard_name=standard_name,
            source_matched_name=matched_name,
            acceptable_ingredient_ids=acceptable_ids,
            expected_result=EXPECTED_FOUND,
            source_language=source_language,
        )
        for index, (
            scenario,
            query,
            ingredient_id,
            standard_name,
            matched_name,
            acceptable_ids,
            source_language,
        ) in enumerate(raw_cases, 1)
    ]
    cases.extend(
        IngredientEvaluationCase(
            case_id=f"IS-{index:03d}",
            query=f"미등록성분검색{index}xyz",
            scenario=NOT_REGISTERED_SCENARIO,
            source_ingredient_id=None,
            source_standard_name=None,
            source_matched_name=None,
            acceptable_ingredient_ids=[],
            expected_result=EXPECTED_EMPTY,
            source_language=None,
        )
        for index in range(
            REGISTERED_CASE_COUNT + 1,
            REGISTERED_CASE_COUNT + NOT_REGISTERED_CASE_COUNT + 1,
        )
    )
    dataset = IngredientEvaluationDataset(
        dataset_version=dataset_version,
        dataset_kind=dataset_kind,
        sampling_seed=seed,
        generated_at=datetime.now(UTC).isoformat(),
        cases=cases,
    )
    _validate_dataset(dataset)
    return dataset


def _excluded_ids(path: Path | None) -> set[int]:
    if path is None:
        return set()
    return {
        case.source_ingredient_id
        for case in load_dataset(path).cases
        if case.source_ingredient_id is not None
    }


async def _run(
    output: Path,
    seed: int,
    exclude_dataset: Path | None,
    dataset_version: str,
    dataset_kind: str,
) -> None:
    settings = DatasetSettings()
    client = await create_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    ingredients, synonyms = await asyncio.gather(
        _fetch_all(client, "ingredients", "ingredient_id,name_kor,name_eng"),
        _fetch_all(client, "synonyms", "synonym_id,ingredient_id,synonym,language"),
    )
    dataset = build_dataset(
        ingredients,
        synonyms,
        seed,
        excluded_ingredient_ids=_excluded_ids(exclude_dataset),
        dataset_version=dataset_version,
        dataset_kind=dataset_kind,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(asdict(dataset), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"성분 검색 평가 초안 {len(dataset.cases)}건: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="성분명 검색 평가 데이터셋 초안 생성")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--exclude-dataset", type=Path)
    parser.add_argument("--dataset-version", default=DATASET_VERSION)
    parser.add_argument("--dataset-kind", default=DEFAULT_DATASET_KIND)
    args = parser.parse_args()
    asyncio.run(
        _run(
            args.output,
            args.seed,
            args.exclude_dataset,
            args.dataset_version,
            args.dataset_kind,
        )
    )


if __name__ == "__main__":
    main()
