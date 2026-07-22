"""고정 JSON 데이터셋으로 실제 Supabase 성분명 검색 성능을 측정한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import subprocess
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import AsyncClient

from app.core.supabase import create_supabase_client
from app.modules.ingredient_search.repository import SupabaseIngredientSearchRepository
from scripts.ingredient_search_dataset import (
    EXPECTED_EMPTY,
    EXPECTED_FOUND,
    IngredientEvaluationCase,
    IngredientEvaluationDataset,
    load_dataset,
)

DEFAULT_DATASET = Path("evaluation/ingredient_search/datasets/draft-v0.1.0.json")
DEFAULT_OUTPUT_DIRECTORY = Path("artifacts/ingredient-search-evaluation")
DEFAULT_REPEAT_COUNT = 5
DEFAULT_RESULT_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_WARMUP_COUNT = 10


class EvaluationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str
    supabase_service_role_key: str


class IngredientSearcher(Protocol):
    async def search_ingredients(self, query: str, limit: int) -> list[Any]: ...


@dataclass(frozen=True)
class SearchObservation:
    case_id: str
    repeat_index: int
    result_ids: list[int]
    latency_ms: float
    error: str | None = None
    fallback_triggered: bool = False
    candidate_pool_truncated: bool = False


class LegacyExactIngredientSearcher:
    """개선 전 이명 완전 일치 방식의 재현용 어댑터."""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def search_ingredients(self, query: str, limit: int) -> list[Any]:
        response = await (
            self._client.table("synonyms")
            .select("ingredient_id")
            .ilike("synonym", _escape_like(query))
            .order("ingredient_id")
            .limit(limit)
            .execute()
        )
        seen: set[int] = set()
        results: list[_LegacyResult] = []
        for row in response.data or []:
            ingredient_id = row.get("ingredient_id") if isinstance(row, dict) else None
            if isinstance(ingredient_id, int) and ingredient_id not in seen:
                seen.add(ingredient_id)
                results.append(_LegacyResult(ingredient_id))
        return results


@dataclass(frozen=True)
class _LegacyResult:
    ingredient_id: int


def build_summary(
    dataset: IngredientEvaluationDataset,
    observations: list[SearchObservation],
) -> dict[str, Any]:
    by_case: dict[str, list[SearchObservation]] = defaultdict(list)
    for observation in observations:
        by_case[observation.case_id].append(observation)
    successful = [item for item in observations if item.error is None]
    canonical = {
        case_id: sorted(items, key=lambda item: item.repeat_index)[0].result_ids
        for case_id, items in by_case.items()
        if items and all(item.error is None for item in items)
    }
    found = [case for case in dataset.cases if case.expected_result == EXPECTED_FOUND]
    empty = [case for case in dataset.cases if case.expected_result == EXPECTED_EMPTY]
    repeat_count = max((item.repeat_index for item in observations), default=0)
    consistent = [
        case
        for case in dataset.cases
        if _is_consistent(by_case.get(case.case_id, []), repeat_count)
    ]
    latencies = [item.latency_ms for item in successful]
    return {
        "dataset_case_count": len(dataset.cases),
        "total_requests": len(observations),
        "request_success_rate": _ratio(len(successful), len(observations)),
        "errors_by_type": dict(
            sorted(Counter(item.error for item in observations if item.error).items())
        ),
        "registered": _quality_metrics(found, canonical),
        "by_scenario": {
            scenario: _quality_metrics(
                [case for case in found if case.scenario == scenario], canonical
            )
            for scenario in sorted({case.scenario for case in found})
        },
        "not_registered": {
            "query_count": len(empty),
            "correct_count": sum(canonical.get(case.case_id) == [] for case in empty),
            "accuracy": _ratio(
                sum(canonical.get(case.case_id) == [] for case in empty), len(empty)
            ),
        },
        "latency_ms": {
            "sample_count": len(latencies),
            "p90": _percentile(latencies, 90),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
        },
        "top5_consistency_rate": _ratio(len(consistent), len(dataset.cases)),
        "fallback_triggered_case_ids": sorted(
            {item.case_id for item in observations if item.fallback_triggered}
        ),
        "candidate_pool_truncated_case_ids": sorted(
            {item.case_id for item in observations if item.candidate_pool_truncated}
        ),
        "inconsistent_case_ids": sorted(
            set(case.case_id for case in dataset.cases)
            - set(case.case_id for case in consistent)
        ),
    }


def _quality_metrics(
    cases: list[IngredientEvaluationCase], canonical: dict[str, list[int]]
) -> dict[str, Any]:
    ranks = [
        _acceptable_rank(canonical.get(case.case_id, []), case.acceptable_ingredient_ids)
        for case in cases
    ]
    return {
        "query_count": len(cases),
        "hit_at_1": _ratio(sum(rank == 1 for rank in ranks), len(cases)),
        "hit_at_5": _ratio(
            sum(rank is not None and rank <= 5 for rank in ranks), len(cases)
        ),
        "hit_at_10": _ratio(
            sum(rank is not None and rank <= 10 for rank in ranks), len(cases)
        ),
        "mrr_at_5": _ratio(
            sum(1 / rank for rank in ranks if rank is not None and rank <= 5), len(cases)
        ),
    }


def _acceptable_rank(result_ids: list[int], acceptable_ids: list[int]) -> int | None:
    acceptable = set(acceptable_ids)
    return next(
        (index for index, ingredient_id in enumerate(result_ids, 1) if ingredient_id in acceptable),
        None,
    )


def _is_consistent(observations: list[SearchObservation], repeat_count: int) -> bool:
    return (
        len(observations) == repeat_count
        and all(item.error is None for item in observations)
        and len({tuple(item.result_ids[:5]) for item in observations}) == 1
    )


def _ratio(numerator: int | float, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


async def _observe(
    searcher: IngredientSearcher,
    case: IngredientEvaluationCase,
    repeat_index: int,
    limit: int,
    timeout: float,
) -> SearchObservation:
    started = perf_counter()
    try:
        results = await asyncio.wait_for(
            searcher.search_ingredients(case.query, limit), timeout=timeout
        )
    except TimeoutError:
        return SearchObservation(
            case.case_id, repeat_index, [], (perf_counter() - started) * 1_000, "timeout"
        )
    except Exception as exc:
        return SearchObservation(
            case.case_id,
            repeat_index,
            [],
            (perf_counter() - started) * 1_000,
            type(exc).__name__,
        )
    return SearchObservation(
        case.case_id,
        repeat_index,
        [result.ingredient_id for result in results],
        (perf_counter() - started) * 1_000,
        fallback_triggered=bool(
            getattr(searcher, "last_ingredient_fallback_triggered", False)
        ),
        candidate_pool_truncated=bool(
            getattr(searcher, "last_ingredient_candidate_pool_truncated", False)
        ),
    )


async def _run(
    args: argparse.Namespace, dataset: IngredientEvaluationDataset
) -> dict[str, Any]:
    if not args.allow_draft and any(case.review_status != "approved" for case in dataset.cases):
        raise SystemExit("승인되지 않은 케이스가 있습니다. 초안 진단은 --allow-draft가 필요합니다.")
    settings = EvaluationSettings()
    client = await create_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    searcher: IngredientSearcher = (
        LegacyExactIngredientSearcher(client)
        if args.strategy == "legacy-exact"
        else SupabaseIngredientSearchRepository(client)
    )
    for case in dataset.cases[: min(args.warmup, len(dataset.cases))]:
        with suppress(Exception):
            await searcher.search_ingredients(case.query, args.limit)
    observations: list[SearchObservation] = []
    for repeat_index in range(1, args.repeats + 1):
        cases = list(dataset.cases)
        random.Random(dataset.sampling_seed + repeat_index).shuffle(cases)
        for case in cases:
            observations.append(
                await _observe(searcher, case, repeat_index, args.limit, args.timeout)
            )
    return {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "label": args.label,
        "strategy": args.strategy,
        "dataset_version": dataset.dataset_version,
        "contains_draft": any(case.review_status != "approved" for case in dataset.cases),
        "search_engine_commit": _git_commit(),
        "config": {
            "repeat_count": args.repeats,
            "result_limit": args.limit,
            "timeout_seconds": args.timeout,
            "warmup_count": args.warmup,
        },
        "summary": build_summary(dataset, observations),
        "observations": [asdict(item) for item in observations],
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="고정 데이터셋 기반 성분명 검색 성능 평가")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--label", default="ingredient-search")
    parser.add_argument("--strategy", choices=("legacy-exact", "improved"), default="improved")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEAT_COUNT)
    parser.add_argument("--limit", type=int, default=DEFAULT_RESULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_COUNT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-draft", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if (
        args.repeats < 1
        or args.limit < DEFAULT_RESULT_LIMIT
        or args.timeout <= 0
        or args.warmup < 0
    ):
        raise SystemExit("repeats는 1 이상, limit은 10 이상, timeout은 양수여야 합니다.")
    dataset = load_dataset(args.dataset)
    report = asyncio.run(_run(args, dataset))
    output = args.output or DEFAULT_OUTPUT_DIRECTORY / f"{args.label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"상세 결과: {output}")


if __name__ == "__main__":
    main()
