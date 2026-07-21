"""실제 Supabase 데이터로 제품명 검색의 기준 성능을 측정한다.

실행:
    uv run python -m scripts.evaluate_product_search

Gemini나 Langfuse 설정 없이 SUPABASE_URL과 SUPABASE_SERVICE_ROLE_KEY만 사용한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import AsyncClient

from app.core.supabase import create_supabase_client
from app.modules.ingredient_search.repository import SupabaseIngredientSearchRepository

CaseKind = Literal["exact", "partial", "no_result"]


class EvaluationSettings(BaseSettings):
    """검색 평가에 필요한 최소 환경 변수."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_url: str
    supabase_service_role_key: str


@dataclass(frozen=True)
class SearchCase:
    kind: CaseKind
    query: str
    expected_product_id: int | None


@dataclass(frozen=True)
class SearchObservation:
    case: SearchCase
    result_ids: list[int]
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class ProductRow:
    id: int
    product_name: str
    main_category: str | None


_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]+")
_BRACKET_PREFIX_PATTERN = re.compile(r"^(?:\s*\[[^]]+\]\s*)+")
_IGNORED_TOKENS = {"new", "단독", "증정", "기획", "공식", "올리브영"}
_NO_RESULT_QUERIES = [
    "코스모스랩 울트라 수분 장벽 크림 137ml",
    "문라이트 보태니컬 진정 세럼 83ml",
    "오로라랩 비타 글로우 토너 142ml",
    "블루코멧 시카 리페어 앰플 47ml",
    "그린웨이브 판테놀 보습 로션 126ml",
    "소프트플래닛 콜라겐 탄력 에센스 61ml",
    "데일리오빗 약산성 클렌징 폼 173ml",
    "퓨어노바 히알루론 수분 젤 92ml",
    "스킨포레스트 세라마이드 장벽 밤 38ml",
    "라이트블룸 나이아신 톤업 크림 74ml",
    "어반듀 알로에 진정 미스트 118ml",
    "클린마스 비타민 수분 패드 67매",
    "벨벳루트 펩타이드 아이 세럼 29ml",
    "모닝스타 녹차 밸런싱 토너 151ml",
    "더마클라우드 마데카 리커버리 크림 88ml",
    "루미너스랩 레티놀 나이트 앰플 43ml",
    "코튼스카이 어성초 카밍 로션 133ml",
    "글로우테라 프로폴리스 영양 에센스 57ml",
    "아쿠아버스 베타글루칸 수딩 젤 104ml",
    "네이처오빗 병풀 데일리 선크림 73ml",
]


def partial_query_from_product_name(product_name: str) -> str:
    """제품명의 장식 문구를 제외하고 읽기 쉬운 첫 토큰 네 글자를 반환한다."""

    normalized = _BRACKET_PREFIX_PATTERN.sub("", product_name).strip()
    tokens = _TOKEN_PATTERN.findall(normalized)
    for token in tokens:
        if token.lower() in _IGNORED_TOKENS or token.isdecimal() or len(token) < 2:
            continue
        return token[:4]
    return normalized[:4]


def build_summary(observations: list[SearchObservation], result_limit: int) -> dict[str, object]:
    """성공한 요청을 기준으로 검색 품질과 지연시간 요약을 만든다."""

    successful = [observation for observation in observations if observation.error is None]
    latencies = [observation.latency_ms for observation in successful]
    exact = [observation for observation in successful if observation.case.kind == "exact"]
    partial = [observation for observation in successful if observation.case.kind == "partial"]
    no_result = [observation for observation in successful if observation.case.kind == "no_result"]

    return {
        "total_queries": len(observations),
        "successful_queries": len(successful),
        "error_count": len(observations) - len(successful),
        "success_rate": _ratio(len(successful), len(observations)),
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "max": max(latencies, default=0.0),
        },
        "exact": _retrieval_summary(exact, result_limit),
        "partial": _partial_retrieval_summary(partial, result_limit),
        "no_result": {
            "query_count": len(no_result),
            "accuracy": _ratio(
                sum(not observation.result_ids for observation in no_result), len(no_result)
            ),
        },
    }


def _retrieval_summary(
    observations: list[SearchObservation], result_limit: int
) -> dict[str, object]:
    ranks = [_expected_rank(observation) for observation in observations]
    return {
        "query_count": len(observations),
        f"hit_at_{result_limit}": _ratio(sum(rank is not None for rank in ranks), len(ranks)),
        f"mrr_at_{result_limit}": _ratio(
            sum(1 / rank for rank in ranks if rank is not None), len(ranks)
        ),
    }


def _partial_retrieval_summary(
    observations: list[SearchObservation], result_limit: int
) -> dict[str, object]:
    """여러 정답이 가능한 부분 검색과 표본 제품의 노출 정도를 함께 표시한다."""

    ranks = [_expected_rank(observation) for observation in observations]
    return {
        "query_count": len(observations),
        "non_empty_rate": _ratio(
            sum(bool(observation.result_ids) for observation in observations), len(observations)
        ),
        f"sample_target_hit_at_{result_limit}": _ratio(
            sum(rank is not None for rank in ranks), len(ranks)
        ),
        f"sample_target_mrr_at_{result_limit}": _ratio(
            sum(1 / rank for rank in ranks if rank is not None), len(ranks)
        ),
    }


def _expected_rank(observation: SearchObservation) -> int | None:
    expected = observation.case.expected_product_id
    if expected is None:
        return None
    try:
        return observation.result_ids.index(expected) + 1
    except ValueError:
        return None


def _ratio(numerator: int | float, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def _fetch_products(client: AsyncClient, page_size: int = 1_000) -> list[ProductRow]:
    products: list[ProductRow] = []
    offset = 0
    while True:
        response = await (
            client.table("products")
            .select("id,product_name,main_category")
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = response.data if isinstance(response.data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            product_id = row.get("id")
            product_name = row.get("product_name")
            category = row.get("main_category")
            if isinstance(product_id, int) and isinstance(product_name, str):
                products.append(
                    ProductRow(
                        id=product_id,
                        product_name=product_name,
                        main_category=category if isinstance(category, str) else None,
                    )
                )
        if len(rows) < page_size:
            return products
        offset += len(rows)


def _category_round_robin(products: list[ProductRow], seed: int) -> list[ProductRow]:
    """특정 대분류에 평가 제품이 몰리지 않도록 결정론적으로 섞는다."""

    groups: dict[str, list[ProductRow]] = defaultdict(list)
    for product in products:
        groups[product.main_category or "(미분류)"].append(product)
    categories = sorted(groups)
    randomizer = random.Random(seed)
    for category in categories:
        randomizer.shuffle(groups[category])
    ordered: list[ProductRow] = []
    index = 0
    while True:
        added = False
        for category in categories:
            if index < len(groups[category]):
                ordered.append(groups[category][index])
                added = True
        if not added:
            return ordered
        index += 1


def _build_cases(
    products: list[ProductRow],
    exact_count: int,
    partial_count: int,
    no_result_count: int,
    seed: int,
) -> list[SearchCase]:
    ordered = _category_round_robin(products, seed)
    if len(ordered) < exact_count + partial_count:
        raise ValueError("요청한 평가 건수보다 products 데이터가 적습니다.")

    exact_products = ordered[:exact_count]
    exact_cases = [
        SearchCase("exact", product.product_name, product.id) for product in exact_products
    ]

    partial_cases: list[SearchCase] = []
    seen_queries: set[str] = set()
    for product in ordered[exact_count:]:
        query = partial_query_from_product_name(product.product_name)
        if len(query) < 2 or query in seen_queries:
            continue
        partial_cases.append(SearchCase("partial", query, product.id))
        seen_queries.add(query)
        if len(partial_cases) == partial_count:
            break
    if len(partial_cases) < partial_count:
        raise ValueError("서로 다른 부분 검색어를 충분히 만들지 못했습니다.")

    if no_result_count > len(_NO_RESULT_QUERIES):
        raise ValueError(f"결과 없음 평가는 최대 {len(_NO_RESULT_QUERIES)}건까지 지원합니다.")
    no_result_cases = [
        SearchCase("no_result", query, None) for query in _NO_RESULT_QUERIES[:no_result_count]
    ]
    return [*exact_cases, *partial_cases, *no_result_cases]


async def _observe(
    repository: SupabaseIngredientSearchRepository, case: SearchCase, limit: int
) -> SearchObservation:
    started = perf_counter()
    try:
        results = await repository.search_products(case.query, limit)
    except Exception as exc:  # 평가 실행은 한 요청 실패 때문에 전체를 중단하지 않는다.
        return SearchObservation(
            case=case,
            result_ids=[],
            latency_ms=(perf_counter() - started) * 1_000,
            error=type(exc).__name__,
        )
    return SearchObservation(
        case=case,
        result_ids=[result.id for result in results],
        latency_ms=(perf_counter() - started) * 1_000,
    )


async def _run(args: argparse.Namespace) -> dict[str, object]:
    settings = EvaluationSettings()  # type: ignore[call-arg]
    client = await create_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    repository = SupabaseIngredientSearchRepository(client)
    products = await _fetch_products(client)
    cases = _build_cases(
        products,
        exact_count=args.exact_count,
        partial_count=args.partial_count,
        no_result_count=args.no_result_count,
        seed=args.seed,
    )

    for _ in range(args.warmup):
        await repository.search_products("크림", args.limit)

    observations = [await _observe(repository, case, args.limit) for case in cases]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "config": {
            "exact_count": args.exact_count,
            "partial_count": args.partial_count,
            "no_result_count": args.no_result_count,
            "result_limit": args.limit,
            "warmup_count": args.warmup,
            "sampling_seed": args.seed,
            "product_source_count": len(products),
        },
        "summary": build_summary(observations, args.limit),
        "observations": [
            {
                "case": asdict(observation.case),
                "result_ids": observation.result_ids,
                "latency_ms": observation.latency_ms,
                "error": observation.error,
            }
            for observation in observations
        ],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supabase 제품명 검색 기준 성능 평가")
    parser.add_argument("--exact-count", type=int, default=40)
    parser.add_argument("--partial-count", type=int, default=40)
    parser.add_argument("--no-result-count", type=int, default=20)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260721)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/search-evaluation/baseline.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if (
        min(
            args.exact_count,
            args.partial_count,
            args.no_result_count,
            args.limit,
            args.warmup,
        )
        < 0
        or args.limit == 0
    ):
        raise SystemExit("평가 건수와 warmup은 0 이상, limit은 1 이상이어야 합니다.")
    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"상세 결과: {args.output}")


if __name__ == "__main__":
    main()
