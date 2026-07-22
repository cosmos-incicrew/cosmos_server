import asyncio
import re
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from supabase import AsyncClient

from app.modules.ingredient_search.repository import (
    ProductSearchDataSourceError,
    SupabaseIngredientSearchRepository,
)


@dataclass
class FakeResponse:
    data: Any


class FakeQuery:
    def __init__(self, data: Any) -> None:
        self._data = data
        self._limit: int | None = None
        self._ilike_patterns: list[str] = []

    def select(self, *columns: str) -> "FakeQuery":
        return self

    def ilike(self, column: str, pattern: str) -> "FakeQuery":
        if column == "product_name":
            self._ilike_patterns.append(pattern)
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
        data = self._data
        if isinstance(data, list) and self._ilike_patterns:
            data = [
                row
                for row in data
                if isinstance(row, dict)
                and isinstance(row.get("product_name"), str)
                and all(
                    _ilike_matches(pattern, row["product_name"])
                    for pattern in self._ilike_patterns
                )
            ]
        if isinstance(data, list) and self._limit is not None:
            return FakeResponse(data[: self._limit])
        return FakeResponse(data)


def _ilike_matches(pattern: str, value: str) -> bool:
    regex = "".join(
        ".*" if character == "%" else "." if character == "_" else re.escape(character)
        for character in pattern
    )
    return re.fullmatch(regex, value, re.IGNORECASE) is not None


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


class FailingFakeQuery(FakeQuery):
    async def execute(self) -> FakeResponse:
        request = httpx.Request("GET", "https://example.supabase.co/rest/v1/products")
        raise httpx.ReadTimeout("Supabase timeout", request=request)


class FailingFakeSupabase(FakeSupabase):
    def table(self, table_name: str) -> FakeQuery:
        if table_name == "products":
            return FailingFakeQuery([])
        return super().table(table_name)


class SlowFakeQuery(FakeQuery):
    async def execute(self) -> FakeResponse:
        await asyncio.sleep(0.02)
        return await super().execute()


class SlowFakeSupabase(FakeSupabase):
    def table(self, table_name: str) -> FakeQuery:
        if table_name == "products":
            return SlowFakeQuery([])
        return super().table(table_name)


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
async def test_repository_does_not_require_every_anchor_to_match() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {"id": 9, "product_name": "더샘 내추럴 마스크팩 알로에"},
                        {"id": 10, "product_name": "다른 브랜드 수분 크림"},
                    ],
                    "product_ingredients": [{"product_id": 9}, {"product_id": 10}],
                }
            ),
        )
    )

    results = await repository.search_products("더샘내추럴마스크팩알로에", 10)

    assert [candidate.id for candidate in results] == [9]
    assert repository.last_search_diagnostics.direct_candidate_count == 0
    assert repository.last_search_diagnostics.tolerant_candidate_count == 1
    assert repository.last_search_diagnostics.merged_candidate_count == 1
    assert repository.last_search_diagnostics.ranked_candidate_count == 1
    assert repository.last_search_diagnostics.direct_query_executed is False
    assert repository.last_search_diagnostics.candidate_pool_truncated is False


@pytest.mark.asyncio
async def test_repository_preserves_literal_lookup_for_compatibility_characters() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [{"id": 11, "product_name": "브랜드 크림 ５０ｍｌ"}],
                    "product_ingredients": [{"product_id": 11}],
                }
            ),
        )
    )

    results = await repository.search_products("크림 ５０ｍｌ", 10)

    assert [candidate.id for candidate in results] == [11]
    assert repository.last_search_diagnostics.direct_query_executed is True


@pytest.mark.asyncio
async def test_repository_runs_direct_query_only_when_tolerant_pool_is_truncated() -> None:
    products = [
        {"id": product_id, "product_name": f"공통 검색 제품 {product_id}"}
        for product_id in range(1, 102)
    ]
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": products,
                    "product_ingredients": [
                        {"product_id": product["id"]} for product in products
                    ],
                }
            ),
        )
    )

    await repository.search_products("공통 검색", 10)

    assert repository.last_search_diagnostics.tolerant_candidate_count == 100
    assert repository.last_search_diagnostics.direct_query_executed is True
    assert repository.last_search_diagnostics.candidate_pool_truncated is True


@pytest.mark.asyncio
async def test_repository_maps_supabase_timeout_and_resets_diagnostics() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(AsyncClient, FailingFakeSupabase({"products": []}))
    )
    repository.last_candidate_pool_truncated = True

    with pytest.raises(ProductSearchDataSourceError):
        await repository.search_products("검색 실패", 10)

    assert repository.last_candidate_pool_truncated is False
    assert repository.last_search_diagnostics.direct_query_executed is False


@pytest.mark.asyncio
async def test_repository_enforces_product_search_timeout() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(AsyncClient, SlowFakeSupabase({"products": []})),
        product_search_timeout_seconds=0.001,
    )

    with pytest.raises(ProductSearchDataSourceError):
        await repository.search_products("느린 검색", 10)


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
                                "name_kor": "테스트 성분",
                                "name_eng": "Test Ingredient",
                            },
                        },
                        {
                            "ingredient_id": "2700",
                            "ingredients": {
                                "ingredient_id": "2700",
                                "name_kor": "테스트 성분",
                                "name_eng": "Test Ingredient",
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
