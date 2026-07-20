from tests.support.mock_supabase import load_product_compare_mock_supabase


async def test_mock_supabase_applies_filters_projection_and_ordering() -> None:
    client = load_product_compare_mock_supabase()

    response = await (
        client.table("product_ingredients")
        .select("product_id,ingredient_id")
        .in_("product_id", [102, 101])
        .order("product_id")
        .order("order_no")
        .order("id")
        .execute()
    )

    assert response.data == [
        {"product_id": 101, "ingredient_id": 1},
        {"product_id": 101, "ingredient_id": 2},
        {"product_id": 101, "ingredient_id": 3},
        {"product_id": 101, "ingredient_id": 4},
        {"product_id": 102, "ingredient_id": 1},
        {"product_id": 102, "ingredient_id": 2},
        {"product_id": 102, "ingredient_id": 3},
        {"product_id": 102, "ingredient_id": 5},
    ]
