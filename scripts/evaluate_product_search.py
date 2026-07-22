"""고정 JSON 데이터셋으로 실제 Supabase 제품명 검색 성능을 측정한다."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import AsyncClient

from app.core.supabase import create_supabase_client
from app.modules.ingredient_search.repository import SupabaseIngredientSearchRepository
from scripts.product_search_dataset import EvaluationCase, EvaluationDataset, load_dataset

DEFAULT_DATASET = Path("evaluation/product_search/datasets/development-v1.0.0.json")
DEFAULT_OUTPUT_DIRECTORY = Path("artifacts/search-evaluation")
DEFAULT_REPEAT_COUNT = 5
DEFAULT_RESULT_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_WARMUP_COUNT = 10
_DB_PAGE_SIZE = 1_000


class EvaluationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str
    supabase_service_role_key: str


@dataclass(frozen=True)
class SearchObservation:
    case_id: str
    repeat_index: int
    result_ids: list[int]
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class DatabaseFingerprint:
    product_count: int
    products_sha256: str
    product_ingredient_count: int


def build_summary(
    dataset: EvaluationDataset, observations: list[SearchObservation]
) -> dict[str, Any]:
    """반복 호출 결과에서 품질·지연시간·안정성 지표를 계산한다."""

    cases_by_id = {case.case_id: case for case in dataset.cases}
    observations_by_case: dict[str, list[SearchObservation]] = defaultdict(list)
    for observation in observations:
        observations_by_case[observation.case_id].append(observation)

    successful = [observation for observation in observations if observation.error is None]
    latencies = [observation.latency_ms for observation in successful]
    canonical_results: dict[str, list[int]] = {}
    for case_id, case_observations in observations_by_case.items():
        ordered = sorted(case_observations, key=lambda item: item.repeat_index)
        if first_success := next((item for item in ordered if item.error is None), None):
            canonical_results[case_id] = first_success.result_ids

    registered = [case for case in dataset.cases if case.expected_result == "found"]
    not_registered = [case for case in dataset.cases if case.expected_result == "empty"]
    maximum_repeat = max((item.repeat_index for item in observations), default=0)
    consistent_case_ids = [
        case.case_id
        for case in dataset.cases
        if _is_consistent(observations_by_case.get(case.case_id, []), maximum_repeat)
    ]

    by_scenario = {
        scenario: _quality_metrics(
            [case for case in registered if case.scenario == scenario], canonical_results
        )
        for scenario in sorted({case.scenario for case in registered})
    }
    by_category = {
        category: _quality_metrics(
            [case for case in registered if case.product_category == category], canonical_results
        )
        for category in sorted(
            {case.product_category for case in registered if case.product_category is not None}
        )
    }
    per_round_p95 = {
        str(repeat): _percentile(
            [item.latency_ms for item in successful if item.repeat_index == repeat], 95
        )
        for repeat in range(1, maximum_repeat + 1)
    }
    repeated_failure_case_ids = sorted(
        case_id
        for case_id, items in observations_by_case.items()
        if sum(item.error is not None for item in items) >= 2
    )

    return {
        "dataset_case_count": len(dataset.cases),
        "total_requests": len(observations),
        "successful_requests": len(successful),
        "error_count": len(observations) - len(successful),
        "errors_by_type": dict(
            sorted(Counter(item.error for item in observations if item.error).items())
        ),
        "request_success_rate": _ratio(len(successful), len(observations)),
        "registered": _quality_metrics(registered, canonical_results),
        "by_scenario": by_scenario,
        "by_category": by_category,
        "not_registered": {
            "query_count": len(not_registered),
            "correct_count": sum(
                canonical_results.get(case.case_id) == [] for case in not_registered
            ),
            "accuracy": _ratio(
                sum(canonical_results.get(case.case_id) == [] for case in not_registered),
                len(not_registered),
            ),
        },
        "latency_ms": {
            "sample_count": len(latencies),
            "p90": _percentile(latencies, 90),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "per_round_p95": per_round_p95,
        },
        "top5_consistent_case_count": len(consistent_case_ids),
        "top5_consistency_rate": _ratio(len(consistent_case_ids), len(dataset.cases)),
        "inconsistent_case_ids": sorted(set(cases_by_id) - set(consistent_case_ids)),
        "repeated_failure_case_ids": repeated_failure_case_ids,
    }


def _quality_metrics(
    cases: list[EvaluationCase], canonical_results: dict[str, list[int]]
) -> dict[str, Any]:
    ranks = [
        _acceptable_rank(canonical_results.get(case.case_id, []), case.acceptable_product_ids)
        for case in cases
    ]
    return {
        "query_count": len(cases),
        "hit_at_1_count": sum(rank is not None and rank <= 1 for rank in ranks),
        "hit_at_1": _ratio(sum(rank is not None and rank <= 1 for rank in ranks), len(cases)),
        "hit_at_5_count": sum(rank is not None and rank <= 5 for rank in ranks),
        "hit_at_5": _ratio(sum(rank is not None and rank <= 5 for rank in ranks), len(cases)),
        "hit_at_10_count": sum(rank is not None and rank <= 10 for rank in ranks),
        "hit_at_10": _ratio(sum(rank is not None and rank <= 10 for rank in ranks), len(cases)),
        "mrr_at_5": _ratio(
            sum(1 / rank for rank in ranks if rank is not None and rank <= 5), len(cases)
        ),
    }


def _acceptable_rank(result_ids: list[int], acceptable_ids: list[int]) -> int | None:
    acceptable = set(acceptable_ids)
    return next(
        (index for index, product_id in enumerate(result_ids, 1) if product_id in acceptable), None
    )


def _is_consistent(observations: list[SearchObservation], repeat_count: int) -> bool:
    if len(observations) != repeat_count or any(item.error for item in observations):
        return False
    top_fives = {tuple(item.result_ids[:5]) for item in observations}
    return len(top_fives) == 1


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
    repository: SupabaseIngredientSearchRepository,
    case: EvaluationCase,
    repeat_index: int,
    limit: int,
    timeout_seconds: float,
) -> SearchObservation:
    started = perf_counter()
    try:
        results = await asyncio.wait_for(
            repository.search_products(case.query, limit), timeout=timeout_seconds
        )
    except TimeoutError:
        return SearchObservation(
            case.case_id, repeat_index, [], (perf_counter() - started) * 1_000, "timeout"
        )
    except Exception as exc:  # 한 요청 오류가 전체 진단 결과를 없애지 않게 기록한다.
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
        [result.id for result in results],
        (perf_counter() - started) * 1_000,
    )


async def database_fingerprint(client: AsyncClient) -> DatabaseFingerprint:
    products: list[tuple[int, str]] = []
    offset = 0
    while True:
        response = await (
            client.table("products")
            .select("id,product_name")
            .order("id")
            .range(offset, offset + _DB_PAGE_SIZE - 1)
            .execute()
        )
        rows = _rows(response.data)
        products.extend(
            (product_id, product_name)
            for row in rows
            if isinstance((product_id := row.get("id")), int)
            and isinstance((product_name := row.get("product_name")), str)
        )
        if len(rows) < _DB_PAGE_SIZE:
            break
        offset += len(rows)

    ingredient_row_count = 0
    offset = 0
    while True:
        response = await (
            client.table("product_ingredients")
            .select("id")
            .order("id")
            .range(offset, offset + _DB_PAGE_SIZE - 1)
            .execute()
        )
        rows = _rows(response.data)
        ingredient_row_count += len(rows)
        if len(rows) < _DB_PAGE_SIZE:
            break
        offset += len(rows)

    digest = hashlib.sha256(
        json.dumps(products, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return DatabaseFingerprint(len(products), digest, ingredient_row_count)


async def _run(args: argparse.Namespace, dataset: EvaluationDataset) -> dict[str, Any]:
    if not args.allow_draft:
        unapproved = [case.case_id for case in dataset.cases if case.review_status != "approved"]
        if unapproved:
            raise SystemExit(
                f"승인되지 않은 평가 케이스가 {len(unapproved)}개 있습니다. "
                "개발 진단만 수행할 때는 --allow-draft를 사용하세요."
            )

    settings = EvaluationSettings()
    client = await create_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    repository = SupabaseIngredientSearchRepository(client)
    start_fingerprint = await database_fingerprint(client)

    for case in dataset.cases[: min(args.warmup, len(dataset.cases))]:
        with suppress(Exception):
            await asyncio.wait_for(
                repository.search_products(case.query, args.limit), timeout=args.timeout
            )

    observations: list[SearchObservation] = []
    for repeat_index in range(1, args.repeats + 1):
        cases = list(dataset.cases)
        random.Random(dataset.sampling_seed + repeat_index).shuffle(cases)
        for case in cases:
            observations.append(
                await _observe(repository, case, repeat_index, args.limit, args.timeout)
            )

    end_fingerprint = await database_fingerprint(client)
    summary = build_summary(dataset, observations)
    return {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "label": args.label,
        "dataset": {
            "path": str(args.dataset),
            "version": dataset.dataset_version,
            "kind": dataset.dataset_kind,
            "case_count": len(dataset.cases),
            "contains_draft": any(case.review_status != "approved" for case in dataset.cases),
        },
        "config": {
            "repeat_count": args.repeats,
            "result_limit": args.limit,
            "timeout_seconds": args.timeout,
            "warmup_count": args.warmup,
            "sequential_requests": True,
        },
        "search_engine_commit": _git_commit(),
        "database": {
            "start": asdict(start_fingerprint),
            "end": asdict(end_fingerprint),
            "unchanged": start_fingerprint == end_fingerprint,
        },
        "summary": summary,
        "observations": [asdict(observation) for observation in observations],
    }


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _rows(data: Any) -> list[dict[str, Any]]:
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="고정 데이터셋 기반 제품명 검색 성능 평가")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEAT_COUNT)
    parser.add_argument("--limit", type=int, default=DEFAULT_RESULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_COUNT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-draft", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.repeats < 1 or args.limit < 10 or args.timeout <= 0 or args.warmup < 0:
        raise SystemExit("repeats는 1 이상, limit은 10 이상, timeout은 양수여야 합니다.")
    dataset = load_dataset(args.dataset)
    report = asyncio.run(_run(args, dataset))
    output = args.output or DEFAULT_OUTPUT_DIRECTORY / (
        f"{args.label}-{dataset.dataset_kind}-v{dataset.dataset_version}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        target.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"상세 결과: {output}")


if __name__ == "__main__":
    main()
