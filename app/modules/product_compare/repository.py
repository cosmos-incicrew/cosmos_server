"""product_compare 모듈의 Supabase 조회 어댑터."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from supabase import AsyncClient

from app.common.restrictions import RestrictionRow, fetch_restriction_rows
from app.core.supabase import get_supabase


@dataclass(frozen=True)
class ProductIngredientRows:
    id: int
    product_name: str
    ingredient_ids: list[int | None]


class ProductCompareRepository(Protocol):
    async def get_products(self, product_ids: list[int]) -> list[ProductIngredientRows]: ...

    async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]: ...

    async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]: ...


class SupabaseProductCompareRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    async def get_products(self, product_ids: list[int]) -> list[ProductIngredientRows]:
        product_response = await (
            self._client.table("products")
            .select("id,product_name")
            .in_("id", product_ids)
            .execute()
        )
        products_by_id = {
            product_id: row
            for row in _rows(product_response.data)
            if (product_id := _integer(row.get("id"))) is not None
            and isinstance(row.get("product_name"), str)
        }
        if not products_by_id:
            return []

        ingredient_response = await (
            self._client.table("product_ingredients")
            .select("product_id,ingredient_id")
            .in_("product_id", product_ids)
            .order("product_id")
            .order("order_no")
            .order("id")
            .execute()
        )
        ingredient_ids_by_product: dict[int, list[int | None]] = {
            product_id: [] for product_id in products_by_id
        }
        for row in _rows(ingredient_response.data):
            product_id = _integer(row.get("product_id"))
            if product_id is not None and product_id in ingredient_ids_by_product:
                ingredient_ids_by_product[product_id].append(_integer(row.get("ingredient_id")))

        return [
            ProductIngredientRows(
                id=product_id,
                product_name=products_by_id[product_id]["product_name"],
                ingredient_ids=ingredient_ids_by_product[product_id],
            )
            for product_id in product_ids
            if product_id in products_by_id
        ]

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


async def get_product_compare_repository() -> ProductCompareRepository:
    return SupabaseProductCompareRepository(await get_supabase())


def _rows(data: Any) -> Sequence[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _integer(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
