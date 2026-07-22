"""ingredient_search 모듈의 Supabase 조회 어댑터."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from supabase import AsyncClient

from app.common.restrictions import RestrictionRow, fetch_restriction_rows
from app.core.supabase import get_supabase
from app.modules.ingredient_search.matching import format_tolerant_like_pattern, rank_candidates
from app.modules.ingredient_search.schemas import (
    IngredientSearchCandidate,
    ProductSearchCandidate,
)

_CANDIDATE_LIMIT_PER_QUERY = 100


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


class SupabaseIngredientSearchRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client
        self.last_candidate_pool_truncated = False

    async def search_products(self, query: str, limit: int) -> list[ProductSearchCandidate]:
        candidate_limit = _CANDIDATE_LIMIT_PER_QUERY
        selection = (
            "id,product_name,brand,main_category,sub_category,detailed_category,product_url,"
            "product_ingredients!inner()"
        )
        direct_query = (
            self._client.table("products")
            .select(selection)
            .not_.is_("product_ingredients.ingredient_id", "null")
            .ilike("product_name", f"%{_escape_like(query)}%")
            .order("product_name")
            .order("id")
            .limit(candidate_limit)
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
        candidate_queries = [direct_query, tolerant_query]

        responses = await asyncio.gather(*(item.execute() for item in candidate_queries))
        candidates_by_id: dict[int, ProductSearchCandidate] = {}
        for response in responses:
            for candidate in _product_candidates(_rows(response.data)):
                candidates_by_id.setdefault(candidate.id, candidate)
        self.last_candidate_pool_truncated = any(
            len(_rows(response.data)) >= candidate_limit for response in responses
        )
        ranked = rank_candidates(query, list(candidates_by_id.values()))
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
