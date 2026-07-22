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
        self._limit: int | None = None

    def select(self, *columns: str) -> "FakeQuery":
        return self

    def ilike(self, column: str, pattern: str) -> "FakeQuery":
        return self

    def or_(self, filters: str) -> "FakeQuery":
        return self

    def order(self, column: str) -> "FakeQuery":
        return self

    def limit(self, count: int) -> "FakeQuery":
        self._limit = count
        return self

    def range(self, from_: int, to: int) -> "FakeQuery":
        return self

    def in_(self, column: str, values: list[int]) -> "FakeQuery":
        return self

    def eq(self, column: str, value: int) -> "FakeQuery":
        return self

    @property
    def not_(self) -> "FakeQuery":
        return self

    def is_(self, column: str, value: str) -> "FakeQuery":
        return self

    def maybe_single(self) -> "FakeQuery":
        return self

    async def execute(self) -> FakeResponse:
        if isinstance(self._data, list) and self._limit is not None:
            return FakeResponse(self._data[: self._limit])
        return FakeResponse(self._data)


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, Any]) -> None:
        self._rows_by_table = rows_by_table

    def table(self, table_name: str) -> FakeQuery:
        data = self._rows_by_table[table_name]
        if table_name == "products" and isinstance(data, list):
            mappings = self._rows_by_table.get("product_ingredients", [])
            mapped_ids = {
                row.get("product_id")
                for row in mappings
                if isinstance(row, dict) and row.get("product_id") is not None
            }
            data = [row for row in data if row.get("id") in mapped_ids]
        return FakeQuery(data)


@pytest.mark.asyncio
async def test_repository_filters_products_without_mapped_ingredients() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "id": 1,
                            "product_name": "분석 가능한 세럼",
                            "brand": "브랜드 A",
                            "main_category": "스킨케어",
                            "sub_category": "세럼",
                            "detailed_category": "페이셜 세럼",
                            "product_url": "https://example.com/products/1",
                        },
                        {
                            "id": 2,
                            "product_name": "성분 없는 세럼",
                            "brand": "브랜드 B",
                            "main_category": "스킨케어",
                            "sub_category": "세럼",
                            "detailed_category": "페이셜 세럼",
                            "product_url": "https://example.com/products/2",
                        },
                    ],
                    "product_ingredients": [{"product_id": 1}],
                }
            ),
        )
    )

    results = await repository.search_products("세럼", 20)

    assert [candidate.model_dump() for candidate in results] == [
        {
            "id": 1,
            "product_name": "분석 가능한 세럼",
            "brand": "브랜드 A",
            "main_category": "스킨케어",
            "sub_category": "세럼",
            "detailed_category": "페이셜 세럼",
            "product_url": "https://example.com/products/1",
        }
    ]


async def test_repository_keeps_each_analyzable_product_in_the_same_flagship_group() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "id": 1,
                            "product_name": "아이크림 35ml",
                            "brand": "브랜드 A",
                            "main_category": "스킨케어",
                            "sub_category": "크림",
                            "detailed_category": "아이크림",
                            "product_url": "https://example.com/products/1",
                            "flagship_id": 1,
                        },
                        {
                            "id": 2,
                            "product_name": "아이크림 기획세트",
                            "brand": "브랜드 A",
                            "main_category": "스킨케어",
                            "sub_category": "크림",
                            "detailed_category": "아이크림",
                            "product_url": "https://example.com/products/2",
                            "flagship_id": 1,
                        },
                    ],
                    "product_ingredients": [{"product_id": 1}, {"product_id": 2}],
                }
            ),
        )
    )

    results = await repository.search_products("아이크림", 20)

    assert [candidate.id for candidate in results] == [1, 2]


@pytest.mark.asyncio
async def test_repository_matches_spacing_and_punctuation_variations() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "id": 7,
                            "product_name": "[단독기획] 허블룸 데일리 톤업 비건 선스크린 50ml",
                            "brand": "허블룸",
                        },
                        {"id": 8, "product_name": "허블룸 수분 크림", "brand": "허블룸"},
                    ],
                    "product_ingredients": [{"product_id": 7}, {"product_id": 8}],
                }
            ),
        )
    )

    results = await repository.search_products("허블룸데일리톤업비건선스크린50ml", 10)

    assert [candidate.id for candidate in results] == [7]


@pytest.mark.asyncio
async def test_repository_prioritizes_core_name_match_over_partial_match() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {"id": 1, "product_name": "아토베리어365 크림 미스트"},
                        {
                            "id": 2,
                            "product_name": "[기획] 에스트라 아토베리어365 크림 80ml (+10ml)",
                        },
                    ],
                    "product_ingredients": [{"product_id": 1}, {"product_id": 2}],
                }
            ),
        )
    )

    results = await repository.search_products("에스트라 아토베리어365 크림", 10)

    assert [candidate.id for candidate in results] == [2]


@pytest.mark.asyncio
async def test_repository_removes_capacity_attached_to_product_name() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [{"id": 1, "product_name": "브랜드 에센스200ml"}],
                    "product_ingredients": [{"product_id": 1}],
                }
            ),
        )
    )

    results = await repository.search_products("브랜드 에센스", 10)

    assert [candidate.id for candidate in results] == [1]


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
                        "id": 1,
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

    result = await repository.get_product_ingredients(1)

    assert result is not None
    assert result.id == 1
    assert result.product_name == "테스트 세럼"
    assert result.ingredient_ids == [2700, 2247, 2700, None]
