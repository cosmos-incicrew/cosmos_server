"""ingredient_search 모듈의 Supabase 조회 어댑터."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

import httpx
from postgrest.exceptions import APIError
from supabase import AsyncClient

from app.common.restrictions import RestrictionRow, fetch_restriction_rows
from app.core.supabase import get_supabase
from app.modules.ingredient_search.matching import (
    format_tolerant_like_pattern,
    rank_candidates,
    requires_literal_product_lookup,
)
from app.modules.ingredient_search.schemas import (
    IngredientSearchCandidate,
    ProductSearchCandidate,
)

_CANDIDATE_LIMIT_PER_QUERY = 100
_PRODUCT_SEARCH_TIMEOUT_SECONDS = 2.0


class ProductSearchDataSourceError(Exception):
    """Supabase 제품 검색을 완료하지 못했습니다."""


class IngredientSearchRepository(Protocol):
    async def search_products(self, query: str, limit: int) -> list[ProductSearchCandidate]: ...

    async def search_ingredients(
        self, query: str, limit: int
    ) -> list[IngredientSearchCandidate]: ...

    async def get_product_ingredients(self, product_id: int) -> "ProductIngredientRows | None": ...

    async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]: ...

    async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]: ...


@dataclass(frozen=True)
class ProductIngredientRows:
    id: int
    product_name: str
    ingredient_ids: list[int | None]


@dataclass(frozen=True)
class ProductSearchDiagnostics:
    direct_candidate_count: int = 0
    tolerant_candidate_count: int = 0
    merged_candidate_count: int = 0
    ranked_candidate_count: int = 0
    direct_query_latency_ms: float = 0.0
    tolerant_query_latency_ms: float = 0.0
    direct_query_executed: bool = False
    candidate_pool_truncated: bool = False


class SupabaseIngredientSearchRepository:
    def __init__(
        self,
        client: AsyncClient,
        product_search_timeout_seconds: float = _PRODUCT_SEARCH_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._product_search_timeout_seconds = product_search_timeout_seconds
        self.last_candidate_pool_truncated = False
        self.last_search_diagnostics = ProductSearchDiagnostics()

    async def search_products(self, query: str, limit: int) -> list[ProductSearchCandidate]:
        self.last_candidate_pool_truncated = False
        self.last_search_diagnostics = ProductSearchDiagnostics()
        candidate_limit = _CANDIDATE_LIMIT_PER_QUERY
        selection = (
            "id,product_name,brand,main_category,sub_category,detailed_category,product_url,"
            "product_ingredients!inner()"
        )
        tolerant_query = (
            self._client.table("products")
            .select(selection)
            .not_.is_("product_ingredients.ingredient_id", "null")
            .ilike("product_name", format_tolerant_like_pattern(query))
            .order("product_name")
            .order("id")
            .limit(candidate_limit)
        )
        direct_response = None
        direct_latency_ms = 0.0
        if requires_literal_product_lookup(query):
            direct_query = _direct_product_query(
                self._client, selection, query, candidate_limit
            )
            timed_responses = await asyncio.gather(
                _execute_with_latency(tolerant_query, self._product_search_timeout_seconds),
                _execute_with_latency(direct_query, self._product_search_timeout_seconds),
            )
            tolerant_response, tolerant_latency_ms = timed_responses[0]
            direct_response, direct_latency_ms = timed_responses[1]
        else:
            tolerant_response, tolerant_latency_ms = await _execute_with_latency(
                tolerant_query, self._product_search_timeout_seconds
            )
        tolerant_count = len(_rows(tolerant_response.data))
        if tolerant_count >= candidate_limit and direct_response is None:
            direct_query = _direct_product_query(
                self._client, selection, query, candidate_limit
            )
            direct_response, direct_latency_ms = await _execute_with_latency(
                direct_query, self._product_search_timeout_seconds
            )

        responses = [tolerant_response]
        if direct_response is not None:
            responses.append(direct_response)
        direct_count = len(_rows(direct_response.data)) if direct_response is not None else 0
        candidates_by_id: dict[int, ProductSearchCandidate] = {}
        for response in responses:
            for candidate in _product_candidates(_rows(response.data)):
                candidates_by_id.setdefault(candidate.id, candidate)
        self.last_candidate_pool_truncated = tolerant_count >= candidate_limit
        ranked = rank_candidates(query, list(candidates_by_id.values()))
        self.last_search_diagnostics = ProductSearchDiagnostics(
            direct_candidate_count=direct_count,
            tolerant_candidate_count=tolerant_count,
            merged_candidate_count=len(candidates_by_id),
            ranked_candidate_count=len(ranked),
            direct_query_latency_ms=direct_latency_ms,
            tolerant_query_latency_ms=tolerant_latency_ms,
            direct_query_executed=direct_response is not None,
            candidate_pool_truncated=self.last_candidate_pool_truncated,
        )
        if not ranked:
            return []
        return ranked[:limit]

    async def search_ingredients(self, query: str, limit: int) -> list[IngredientSearchCandidate]:
        synonym_response = await (
            self._client.table("synonyms")
            .select("ingredient_id,ingredients!inner(ingredient_id,name_kr,name_en)")
            .ilike("synonym", _escape_like(query))
            .order("ingredient_id")
            .limit(limit)
            .execute()
        )
        results_by_id: dict[int, IngredientSearchCandidate] = {}
        for row in _rows(synonym_response.data):
            ingredient = _embedded_row(row.get("ingredients"))
            ingredient_id = _integer(ingredient.get("ingredient_id"))
            name_kr = ingredient.get("name_kr")
            if ingredient_id is None or not isinstance(name_kr, str):
                continue
            results_by_id.setdefault(
                ingredient_id,
                IngredientSearchCandidate(
                    ingredient_id=ingredient_id,
                    name_kr=name_kr,
                    name_en=_optional_text(ingredient.get("name_en")),
                ),
            )
        return list(results_by_id.values())

    async def get_product_ingredients(self, product_id: int) -> ProductIngredientRows | None:
        product_response = await (
            self._client.table("products")
            .select("id,product_name")
            .eq("id", product_id)
            .maybe_single()
            .execute()
        )
        if product_response is None or not isinstance(product_response.data, dict):
            return None
        product = product_response.data
        resolved_product_id = _integer(product.get("id"))
        product_name = product.get("product_name")
        if resolved_product_id is None or not isinstance(product_name, str):
            return None

        ingredient_response = await (
            self._client.table("product_ingredients")
            .select("ingredient_id")
            .eq("product_id", product_id)
            .order("order_no")
            .order("id")
            .execute()
        )
        return ProductIngredientRows(
            id=resolved_product_id,
            product_name=product_name,
            ingredient_ids=[
                _integer(row.get("ingredient_id")) for row in _rows(ingredient_response.data)
            ],
        )

    async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]:
        if not ingredient_ids:
            return {}
        response = await (
            self._client.table("ingredients")
            .select("ingredient_id,name_kor")
            .in_("ingredient_id", ingredient_ids)
            .execute()
        )
        return {
            ingredient_id: name_kor
            for row in _rows(response.data)
            if (ingredient_id := _integer(row.get("ingredient_id"))) is not None
            and isinstance((name_kor := row.get("name_kor")), str)
        }

    async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]:
        return await fetch_restriction_rows(self._client, ingredient_ids)


async def get_ingredient_search_repository() -> IngredientSearchRepository:
    return SupabaseIngredientSearchRepository(await get_supabase())


def _rows(data: Any) -> Sequence[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _product_candidates(rows: Sequence[dict[str, Any]]) -> list[ProductSearchCandidate]:
    return [
        ProductSearchCandidate(
            id=product_id,
            product_name=product_name,
            brand=_optional_text(row.get("brand")),
            main_category=_optional_text(row.get("main_category")),
            sub_category=_optional_text(row.get("sub_category")),
            detailed_category=_optional_text(row.get("detailed_category")),
            product_url=_optional_text(row.get("product_url")),
        )
        for row in rows
        if (product_id := _integer(row.get("id"))) is not None
        and isinstance((product_name := row.get("product_name")), str)
    ]


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _embedded_row(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _integer(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _direct_product_query(client: AsyncClient, selection: str, query: str, limit: int) -> Any:
    return (
        client.table("products")
        .select(selection)
        .not_.is_("product_ingredients.ingredient_id", "null")
        .ilike("product_name", f"%{_escape_like(query)}%")
        .order("product_name")
        .order("id")
        .limit(limit)
    )


async def _execute_with_latency(query: Any, timeout_seconds: float) -> tuple[Any, float]:
    started = perf_counter()
    try:
        response = await asyncio.wait_for(query.execute(), timeout=timeout_seconds)
    except (APIError, httpx.HTTPError, TimeoutError) as exc:
        raise ProductSearchDataSourceError from exc
    return response, (perf_counter() - started) * 1_000
