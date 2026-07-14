from typing import cast

from supabase import AsyncClient

from app.modules.product_compare.repository import SupabaseProductCompareRepository
from tests.support.mock_supabase import load_product_compare_mock_supabase


async def test_repository_preserves_requested_product_and_ingredient_order() -> None:
    repository = SupabaseProductCompareRepository(
        cast(AsyncClient, load_product_compare_mock_supabase())
    )

    products = await repository.get_products([102, 101])

    assert [(product.id, product.ingredient_ids) for product in products] == [
        (102, [1, 2, 3, 5]),
        (101, [1, 2, 3, 4]),
    ]


async def test_repository_preserves_nullable_ingredient_mapping() -> None:
    repository = SupabaseProductCompareRepository(
        cast(AsyncClient, load_product_compare_mock_supabase())
    )

    products = await repository.get_products([105])

    assert [(product.id, product.ingredient_ids) for product in products] == [(105, [None])]


async def test_repository_maps_ingredient_names_and_restriction_rows() -> None:
    repository = SupabaseProductCompareRepository(
        cast(AsyncClient, load_product_compare_mock_supabase())
    )

    names = await repository.get_ingredient_names([1, 2])
    restrictions = await repository.get_restrictions([1, 2])

    assert names == {1: "정제수", 2: "글리세린"}
    assert [restriction.restriction_id for restriction in restrictions] == [10, 11]
    assert restrictions[0].ingredient_id == 2
