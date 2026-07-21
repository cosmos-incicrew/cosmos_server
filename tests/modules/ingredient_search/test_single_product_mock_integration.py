from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from supabase import AsyncClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.ingredient_search.repository import (
    SupabaseIngredientSearchRepository,
    get_ingredient_search_repository,
)
from tests.support.mock_supabase import load_product_compare_mock_supabase


@pytest.fixture()
def client_with_mock_supabase() -> AsyncIterator[TestClient]:
    repository = SupabaseIngredientSearchRepository(
        cast(AsyncClient, load_product_compare_mock_supabase())
    )
    app.dependency_overrides[verify_jwt] = lambda: "user-123"
    app.dependency_overrides[get_ingredient_search_repository] = lambda: repository
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_selected_product_returns_restrictions_from_supabase_shaped_mock(
    client_with_mock_supabase: TestClient,
) -> None:
    response = client_with_mock_supabase.get("/api/v1/products/101/ingredients")

    assert response.status_code == 200
    assert response.json() == {
        "id": 101,
        "product_name": "제품 A",
        "ingredient_ids": [1, 2, 3, 4],
        "mapped_ingredient_count": 4,
        "unmapped_ingredient_count": 0,
        "restricted_ingredients": [
            {
                "ingredient_id": 2,
                "name_kr": "글리세린",
                "restrictions": [
                    {
                        "restriction_id": 10,
                        "regulate_type": "한도",
                        "provis_atrcl": "사용 조건",
                        "limit_cond": "배합 한도",
                        "is_registered_korea": True,
                    },
                    {
                        "restriction_id": 11,
                        "regulate_type": "금지",
                        "provis_atrcl": "예외 조항",
                        "limit_cond": None,
                        "is_registered_korea": True,
                    },
                ],
            }
        ],
    }
