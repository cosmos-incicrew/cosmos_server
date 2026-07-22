import asyncio
import re
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from supabase import AsyncClient

from app.modules.ingredient_search.ingredient_matching import normalize_ingredient_text
from app.modules.ingredient_search.repository import (
    IngredientSearchDataSourceError,
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
        self._ilike_patterns: list[tuple[str, str]] = []

    def select(self, *columns: str) -> "FakeQuery":
        return self

    def ilike(self, column: str, pattern: str) -> "FakeQuery":
        self._ilike_patterns.append((column, pattern))
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
                and all(
                    isinstance(row.get(column), str)
                    and _ilike_matches(pattern, row[column])
                    for column, pattern in self._ilike_patterns
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
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []

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

    def rpc(self, function_name: str, params: dict[str, Any]) -> FakeQuery:
        self.rpc_calls.append((function_name, params))
        assert function_name == "search_ingredient_candidates"
        query = normalize_ingredient_text(str(params["search_query"]))
        limit = int(params["result_limit"])
        sources: dict[str, list[dict[str, Any]]] = {
            "name_kor": [],
            "name_eng": [],
            "synonym": [],
        }
        for row in self._rows_by_table.get("ingredients", []):
            if not isinstance(row, dict):
                continue
            for column in ("name_kor", "name_eng"):
                value = row.get(column)
                if isinstance(value, str) and query in normalize_ingredient_text(value):
                    sources[column].append(
                        {
                            "ingredient_id": row.get("ingredient_id"),
                            "name_kor": row.get("name_kor"),
                            "name_eng": row.get("name_eng"),
                            "synonym": None,
                            "match_source": f"standard_{column}",
                            "match_value": value,
                        }
                    )
        ingredients_by_id = {
            row.get("ingredient_id"): row
            for row in self._rows_by_table.get("ingredients", [])
            if isinstance(row, dict)
        }
        for row in self._rows_by_table.get("synonyms", []):
            if not isinstance(row, dict):
                continue
            value = row.get("synonym")
            if not isinstance(value, str) or query not in normalize_ingredient_text(value):
                continue
            ingredient = row.get("ingredients")
            if not isinstance(ingredient, dict):
                ingredient = ingredients_by_id.get(row.get("ingredient_id"), {})
            sources["synonym"].append(
                {
                    "ingredient_id": row.get("ingredient_id"),
                    "name_kor": ingredient.get("name_kor"),
                    "name_eng": ingredient.get("name_eng"),
                    "synonym": value,
                    "match_source": "synonym",
                    "match_value": value,
                }
            )
        results: list[dict[str, Any]] = []
        for rows in sources.values():
            rows.sort(key=lambda row: _fake_rpc_match_key(query, str(row["match_value"])))
            for row in rows[: limit + 1]:
                row.pop("match_value")
                row["source_match_count"] = None
                results.append(row)
        return FakeQuery(results)


def _fake_rpc_match_key(query: str, value: str) -> tuple[int, int, str]:
    normalized = normalize_ingredient_text(value)
    tier = 0 if normalized == query else 1 if normalized.startswith(query) else 2
    return tier, len(normalized), normalized


class FailingFakeQuery(FakeQuery):
    async def execute(self) -> FakeResponse:
        request = httpx.Request("GET", "https://example.supabase.co/rest/v1/products")
        raise httpx.ReadTimeout("Supabase timeout", request=request)


class FailingFakeSupabase(FakeSupabase):
    def table(self, table_name: str) -> FakeQuery:
        if table_name == "products":
            return FailingFakeQuery([])
        return super().table(table_name)


class FailingIngredientFakeSupabase(FakeSupabase):
    def table(self, table_name: str) -> FakeQuery:
        if table_name in {"ingredients", "synonyms"}:
            return FailingFakeQuery([])
        return super().table(table_name)

    def rpc(self, function_name: str, params: dict[str, Any]) -> FakeQuery:
        return FailingFakeQuery([])


class SlowFakeQuery(FakeQuery):
    async def execute(self) -> FakeResponse:
        await asyncio.sleep(0.02)
        return await super().execute()


class SlowFakeSupabase(FakeSupabase):
    def table(self, table_name: str) -> FakeQuery:
        if table_name == "products":
            return SlowFakeQuery([])
        return super().table(table_name)


class SlowIngredientFakeSupabase(FakeSupabase):
    def table(self, table_name: str) -> FakeQuery:
        if table_name in {"ingredients", "synonyms"}:
            return SlowFakeQuery([])
        return super().table(table_name)

    def rpc(self, function_name: str, params: dict[str, Any]) -> FakeQuery:
        return SlowFakeQuery([])


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
                            "cleaned_product_name": "분석 가능한 세럼",
                            "brand": "브랜드 A",
                            "main_category": "스킨케어",
                            "sub_category": "세럼",
                            "detailed_category": "페이셜 세럼",
                            "product_url": "https://example.com/products/1",
                        },
                        {
                            "id": 2,
                            "product_name": "성분 없는 세럼",
                            "cleaned_product_name": "성분 없는 세럼",
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
                            "cleaned_product_name": "아이크림",
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
                            "cleaned_product_name": "아이크림",
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
                            "cleaned_product_name": "허블룸 데일리 톤업 비건 선스크린",
                            "brand": "허블룸",
                        },
                        {
                            "id": 8,
                            "product_name": "허블룸 수분 크림",
                            "cleaned_product_name": "허블룸 수분 크림",
                            "brand": "허블룸",
                        },
                    ],
                    "product_ingredients": [{"product_id": 7}, {"product_id": 8}],
                }
            ),
        )
    )

    results = await repository.search_products("허블룸데일리톤업비건선스크린", 10)

    assert [candidate.id for candidate in results] == [7]


@pytest.mark.asyncio
async def test_repository_does_not_require_every_anchor_to_match() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "id": 9,
                            "product_name": "더샘 내추럴 마스크팩 알로에",
                            "cleaned_product_name": "더샘 내추럴 마스크팩 알로에",
                        },
                        {
                            "id": 10,
                            "product_name": "다른 브랜드 수분 크림",
                            "cleaned_product_name": "다른 브랜드 수분 크림",
                        },
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
                    "products": [
                        {
                            "id": 11,
                            "product_name": "[한정] 브랜드 크림 Ｂ５",
                            "cleaned_product_name": "브랜드 크림 Ｂ５",
                        }
                    ],
                    "product_ingredients": [{"product_id": 11}],
                }
            ),
        )
    )

    results = await repository.search_products("크림 Ｂ５", 10)

    assert [candidate.id for candidate in results] == [11]
    assert repository.last_search_diagnostics.direct_query_executed is True


@pytest.mark.asyncio
async def test_repository_runs_direct_query_only_when_tolerant_pool_is_truncated() -> None:
    products = [
        {
            "id": product_id,
            "product_name": f"[기획] 공통 검색 제품 {product_id}",
            "cleaned_product_name": f"공통 검색 제품 {product_id}",
        }
        for product_id in range(1, 102)
    ]
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": products,
                    "product_ingredients": [{"product_id": product["id"]} for product in products],
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
        search_timeout_seconds=0.001,
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
                        {
                            "id": 1,
                            "product_name": "아토베리어365 크림 미스트",
                            "cleaned_product_name": "아토베리어365 크림 미스트",
                        },
                        {
                            "id": 2,
                            "product_name": "[기획] 에스트라 아토베리어365 크림 80ml (+10ml)",
                            "cleaned_product_name": "에스트라 아토베리어365 크림",
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
async def test_repository_searches_cleaned_name_and_returns_original_name() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "id": 1,
                            "product_name": "[단독기획] 브랜드 에센스200ml",
                            "cleaned_product_name": "브랜드 에센스",
                        }
                    ],
                    "product_ingredients": [{"product_id": 1}],
                }
            ),
        )
    )

    results = await repository.search_products("브랜드 에센스", 10)

    assert [candidate.id for candidate in results] == [1]
    assert results[0].product_name == "[단독기획] 브랜드 에센스200ml"


@pytest.mark.asyncio
async def test_repository_does_not_search_removed_marketing_text() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {
                            "id": 1,
                            "product_name": "[단독기획] 브랜드 에센스200ml",
                            "cleaned_product_name": "브랜드 에센스",
                        }
                    ],
                    "product_ingredients": [{"product_id": 1}],
                }
            ),
        )
    )

    assert await repository.search_products("단독기획", 10) == []


@pytest.mark.asyncio
async def test_repository_deduplicates_alias_matches_by_integer_ingredient_id() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "ingredients": [],
                    "synonyms": [
                        {
                            "ingredient_id": "2700",
                            "synonym": "테스트 이명",
                            "ingredients": {
                                "ingredient_id": "2700",
                                "name_kor": "테스트 성분",
                                "name_eng": "Test Ingredient",
                            },
                        },
                        {
                            "ingredient_id": "2700",
                            "synonym": "테스트 이명",
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
async def test_repository_searches_partial_standard_names_and_synonyms() -> None:
    client = FakeSupabase(
        {
            "ingredients": [
                {
                    "ingredient_id": 1,
                    "name_kor": "판테놀",
                    "name_eng": "Panthenol",
                },
                {
                    "ingredient_id": 2,
                    "name_kor": "덱스판테놀",
                    "name_eng": "Dexpanthenol",
                },
            ],
            "synonyms": [
                {
                    "ingredient_id": 3,
                    "synonym": "판테놀 전구체",
                    "ingredients": {
                        "ingredient_id": 3,
                        "name_kor": "디판테놀",
                        "name_eng": "D-Panthenol",
                    },
                }
            ],
        }
    )
    repository = SupabaseIngredientSearchRepository(cast(AsyncClient, client))

    results = await repository.search_ingredients("판테", 20)

    assert [candidate.ingredient_id for candidate in results] == [1, 3, 2]
    assert client.rpc_calls == [
        (
            "search_ingredient_candidates",
            {"search_query": "판테", "result_limit": 100},
        )
    ]


@pytest.mark.asyncio
async def test_repository_prioritizes_exact_synonym_over_partial_standard_name() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "ingredients": [
                        {
                            "ingredient_id": 1,
                            "name_kor": "비타민C유도체",
                            "name_eng": None,
                        }
                    ],
                    "synonyms": [
                        {
                            "ingredient_id": 2,
                            "synonym": "비타민 C",
                            "ingredients": {
                                "ingredient_id": 2,
                                "name_kor": "아스코빅애씨드",
                                "name_eng": "Ascorbic Acid",
                            },
                        }
                    ],
                }
            ),
        )
    )

    results = await repository.search_ingredients("비타민C", 20)

    assert [candidate.ingredient_id for candidate in results] == [2, 1]


@pytest.mark.asyncio
async def test_repository_maps_ingredient_search_supabase_failure() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FailingIngredientFakeSupabase({"ingredients": [], "synonyms": []}),
        )
    )

    with pytest.raises(IngredientSearchDataSourceError):
        await repository.search_ingredients("판테놀", 20)


@pytest.mark.asyncio
async def test_repository_prioritizes_exact_ingredient_when_candidate_pool_is_truncated() -> None:
    ingredients = [
            {
                "ingredient_id": ingredient_id,
                "name_kor": f"오이{ingredient_id}",
                "name_eng": None,
            }
        for ingredient_id in range(1, 101)
    ]
    ingredients.append({"ingredient_id": 101, "name_kor": "오이", "name_eng": None})
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase({"ingredients": ingredients, "synonyms": []}),
        )
    )

    results = await repository.search_ingredients("오이", 10)

    assert results[0].ingredient_id == 101
    assert repository.last_ingredient_fallback_triggered is False
    assert repository.last_ingredient_candidate_pool_truncated is True


@pytest.mark.asyncio
async def test_repository_normalizes_format_without_broad_character_gap_candidates() -> None:
    ingredients = [
        {
            "ingredient_id": ingredient_id,
            "name_kor": f"테스트성분{ingredient_id}",
            "name_eng": f"S{ingredient_id}o{ingredient_id}diumal",
        }
        for ingredient_id in range(1, 151)
    ]
    ingredients.append(
        {
            "ingredient_id": 151,
            "name_kor": "소듐알룸",
            "name_eng": "Sodium Alum",
        }
    )
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            FakeSupabase({"ingredients": ingredients, "synonyms": []}),
        )
    )

    results = await repository.search_ingredients("sodiumal", 10)

    assert results[0].ingredient_id == 151
    assert repository.last_ingredient_fallback_triggered is False
    assert repository.last_ingredient_candidate_pool_truncated is False


@pytest.mark.asyncio
async def test_repository_enforces_total_ingredient_search_timeout() -> None:
    repository = SupabaseIngredientSearchRepository(
        cast(
            AsyncClient,
            SlowIngredientFakeSupabase({"ingredients": [], "synonyms": []}),
        ),
        search_timeout_seconds=0.001,
    )

    with pytest.raises(IngredientSearchDataSourceError):
        await repository.search_ingredients("판테놀", 20)


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
