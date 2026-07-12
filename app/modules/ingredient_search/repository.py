"""ingredient_search 모듈의 Supabase 조회 어댑터."""

from collections.abc import Sequence
from typing import Any, Protocol

from supabase import AsyncClient

from app.core.supabase import get_supabase
from app.modules.ingredient_search.schemas import (
    IngredientSearchCandidate,
    ProductIngredientIdsResponse,
    ProductSearchCandidate,
)


class IngredientSearchRepository(Protocol):
    async def search_products(self, query: str, limit: int) -> list[ProductSearchCandidate]: ...

    async def search_ingredients(
        self, query: str, limit: int
    ) -> list[IngredientSearchCandidate]: ...

    async def get_product_ingredient_ids(
        self, product_id: str
    ) -> ProductIngredientIdsResponse | None: ...


class SupabaseIngredientSearchRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def search_products(self, query: str, limit: int) -> list[ProductSearchCandidate]:
        product_response = await (
            self._client.table("products")
            .select("product_id,product_name,main_category,sub_category")
            .ilike("product_name", f"%{_escape_like(query)}%")
            .order("product_name")
            .order("product_id")
            .limit(limit)
            .execute()
        )
        product_rows = _rows(product_response.data)
        product_ids = [
            row["product_id"] for row in product_rows if isinstance(row.get("product_id"), str)
        ]
        if not product_ids:
            return []

        mapped_response = await (
            self._client.table("product_ingredients")
            .select("product_id")
            .in_("product_id", product_ids)
            .not_.is_("ingredient_id", "null")
            .execute()
        )
        analyzable_product_ids = {
            row["product_id"]
            for row in _rows(mapped_response.data)
            if isinstance(row.get("product_id"), str)
        }
        return [
            ProductSearchCandidate(
                product_id=row["product_id"],
                product_name=row["product_name"],
                main_category=_optional_text(row.get("main_category")),
                sub_category=_optional_text(row.get("sub_category")),
            )
            for row in product_rows
            if row.get("product_id") in analyzable_product_ids
            and isinstance(row.get("product_name"), str)
        ]

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

    async def get_product_ingredient_ids(
        self, product_id: str
    ) -> ProductIngredientIdsResponse | None:
        product_response = await (
            self._client.table("products")
            .select("product_id,product_name")
            .eq("product_id", product_id)
            .maybe_single()
            .execute()
        )
        if product_response is None or not isinstance(product_response.data, dict):
            return None
        product = product_response.data
        resolved_product_id = product.get("product_id")
        product_name = product.get("product_name")
        if not isinstance(resolved_product_id, str) or not isinstance(product_name, str):
            return None

        ingredient_response = await (
            self._client.table("product_ingredients")
            .select("ingredient_id")
            .eq("product_id", product_id)
            .order("order_no")
            .order("id")
            .execute()
        )
        ingredient_ids: list[int] = []
        seen_ids: set[int] = set()
        unmapped_ingredient_count = 0
        for row in _rows(ingredient_response.data):
            ingredient_id = _integer(row.get("ingredient_id"))
            if ingredient_id is None:
                unmapped_ingredient_count += 1
                continue
            if ingredient_id not in seen_ids:
                seen_ids.add(ingredient_id)
                ingredient_ids.append(ingredient_id)

        return ProductIngredientIdsResponse(
            product_id=resolved_product_id,
            product_name=product_name,
            ingredient_ids=ingredient_ids,
            mapped_ingredient_count=len(ingredient_ids),
            unmapped_ingredient_count=unmapped_ingredient_count,
        )


async def get_ingredient_search_repository() -> IngredientSearchRepository:
    return SupabaseIngredientSearchRepository(await get_supabase())


def _rows(data: Any) -> Sequence[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


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
