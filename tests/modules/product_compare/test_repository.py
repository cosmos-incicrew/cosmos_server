from dataclasses import dataclass
from typing import Any, cast

import pytest
from supabase import AsyncClient

from app.modules.product_compare.repository import SupabaseProductCompareRepository


@dataclass
class FakeResponse:
    data: Any


class FakeQuery:
    def __init__(self, data: Any) -> None:
        self._data = data

    def select(self, *columns: str) -> "FakeQuery":
        return self

    def in_(self, column: str, values: list[int]) -> "FakeQuery":
        return self

    def order(self, column: str) -> "FakeQuery":
        return self

    async def execute(self) -> FakeResponse:
        return FakeResponse(self._data)


class FakeSupabase:
    def __init__(self, rows_by_table: dict[str, Any]) -> None:
        self._rows_by_table = rows_by_table

    def table(self, table_name: str) -> FakeQuery:
        return FakeQuery(self._rows_by_table[table_name])


@pytest.mark.asyncio
async def test_repository_preserves_requested_product_order_and_raw_ingredient_rows() -> None:
    repository = SupabaseProductCompareRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "products": [
                        {"id": 102, "product_name": "제품 B"},
                        {"id": 101, "product_name": "제품 A"},
                    ],
                    "product_ingredients": [
                        {"product_id": 101, "ingredient_id": "1"},
                        {"product_id": 101, "ingredient_id": None},
                        {"product_id": 102, "ingredient_id": "2"},
                    ],
                }
            ),
        )
    )

    products = await repository.get_products([101, 102])

    assert [(product.id, product.ingredient_ids) for product in products] == [
        (101, [1, None]),
        (102, [2]),
    ]


@pytest.mark.asyncio
async def test_repository_maps_ingredient_names_and_restriction_rows() -> None:
    repository = SupabaseProductCompareRepository(
        cast(
            AsyncClient,
            FakeSupabase(
                {
                    "ingredients": [
                        {"ingredient_id": "1", "name_kr": "정제수"},
                        {"ingredient_id": "2", "name_kr": "글리세린"},
                    ],
                    "restrictions": [
                        {
                            "restriction_id": "10",
                            "ingredient_id": "2",
                            "regulate_type": "한도",
                            "provis_atrcl": "사용 조건",
                            "limit_cond": "배합 한도",
                            "is_registered_korea": True,
                        }
                    ],
                }
            ),
        )
    )

    names = await repository.get_ingredient_names([1, 2])
    restrictions = await repository.get_restrictions([1, 2])

    assert names == {1: "정제수", 2: "글리세린"}
    assert [restriction.restriction_id for restriction in restrictions] == [10]
    assert restrictions[0].ingredient_id == 2
