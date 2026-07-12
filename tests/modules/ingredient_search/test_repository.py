from dataclasses import dataclass
from typing import Any, cast

import pytest
from supabase import AsyncClient

from app.modules.ingredient_search.repository import SupabaseIngredientSearchRepository


@dataclass
class FakeResponse:
    data: Any


class FakeQuery:
    def __init__(self, data: Any) -> None:
        self._data = data

    def select(self, *columns: str) -> "FakeQuery":
        return self

    def ilike(self, column: str, pattern: str) -> "FakeQuery":
        return self

    def order(self, column: str) -> "FakeQuery":
        return self

    def limit(self, count: int) -> "FakeQuery":
        return self

    def in_(self, column: str, values: list[str]) -> "FakeQuery":
        return self

    def eq(self, column: str, value: str) -> "FakeQuery":
        return self

    @property
    def not_(self) -> "FakeQuery":
        return self

    def is_(self, column: str, value: str) -> "FakeQuery":
        return self

    def maybe_single(self) -> "FakeQuery":
        return self

    async def execute(self) -> FakeResponse:
        return FakeResponse(self._data)


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, Any]) -> None:
        self._rows_by_table = rows_by_table

    def table(self, table_name: str) -> FakeQuery:
        return FakeQuery(self._rows_by_table[table_name])


@pytest.mark.asyncio
async def test_repository_filters_products_without_mapped_ingredients() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "product_id": "product-001",
                            "product_name": "분석 가능한 세럼",
                            "main_category": "스킨케어",
                            "sub_category": "세럼",
                        },
                        {
                            "product_id": "product-002",
                            "product_name": "성분 없는 세럼",
                            "main_category": "스킨케어",
                            "sub_category": "세럼",
                        },
                    ],
                    "product_ingredients": [{"product_id": "product-001"}],
                }
            ),
        )
    )

    results = await repository.search_products("세럼", 20)

    assert [candidate.model_dump() for candidate in results] == [
        {
            "product_id": "product-001",
            "product_name": "분석 가능한 세럼",
            "main_category": "스킨케어",
            "sub_category": "세럼",
        }
    ]


@pytest.mark.asyncio
async def test_repository_deduplicates_alias_matches_by_integer_ingredient_id() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "synonyms": [
                        {
                            "ingredient_id": "2700",
                            "ingredients": {
                                "ingredient_id": "2700",
                                "name_kr": "테스트 성분",
                                "name_en": "Test Ingredient",
                            },
                        },
                        {
                            "ingredient_id": "2700",
                            "ingredients": {
                                "ingredient_id": "2700",
                                "name_kr": "테스트 성분",
                                "name_en": "Test Ingredient",
                            },
                        },
                    ]
                }
            ),
        )
    )

    results = await repository.search_ingredients("테스트 이명", 20)

    assert [candidate.model_dump() for candidate in results] == [
        {
            "ingredient_id": 2700,
            "name_kr": "테스트 성분",
            "name_en": "Test Ingredient",
        }
    ]


@pytest.mark.asyncio
async def test_repository_returns_ordered_unique_integer_ids_for_a_product() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": {
                        "product_id": "product-001",
                        "product_name": "테스트 세럼",
                    },
                    "product_ingredients": [
                        {"ingredient_id": "2700"},
                        {"ingredient_id": "2247"},
                        {"ingredient_id": "2700"},
                        {"ingredient_id": None},
                    ],
                }
            ),
        )
    )

    result = await repository.get_product_ingredient_ids("product-001")

    assert result is not None
    assert result.model_dump() == {
        "product_id": "product-001",
        "product_name": "테스트 세럼",
        "ingredient_ids": [2700, 2247],
        "mapped_ingredient_count": 2,
        "unmapped_ingredient_count": 1,
    }
