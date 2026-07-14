from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from supabase import AsyncClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.product_compare.repository import (
    ProductCompareRepository,
    ProductIngredientRows,
    RestrictionRow,
    SupabaseProductCompareRepository,
    get_product_compare_repository,
)
from app.modules.product_compare.schemas import MIN_COMPARE_PRODUCT_COUNT
from tests.support.mock_supabase import load_product_compare_mock_supabase


class StubProductCompareRepository(ProductCompareRepository):
    async def get_products(self, product_ids: list[int]) -> list[ProductIngredientRows]:
        raise NotImplementedError

    async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]:
        return {ingredient_id: f"성분 {ingredient_id}" for ingredient_id in ingredient_ids}

    async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]:
        return []


@pytest.fixture()
def client() -> AsyncIterator[TestClient]:
    app.dependency_overrides[verify_jwt] = lambda: "user-123"
    app.dependency_overrides[get_product_compare_repository] = lambda: (
        SupabaseProductCompareRepository(cast(AsyncClient, load_product_compare_mock_supabase()))
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_compare_products_returns_presence_and_resolved_ids(client: TestClient) -> None:
    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": [101, 102]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "products": [
            {"id": 101, "product_name": "제품 A"},
            {"id": 102, "product_name": "제품 B"},
        ],
        "ingredient_presence": [
            {
                "ingredient_id": 1,
                "name_kr": "정제수",
                "product_ids": [101, 102],
                "presence_type": "all",
                "restrictions": [],
            },
            {
                "ingredient_id": 2,
                "name_kr": "글리세린",
                "product_ids": [101, 102],
                "presence_type": "all",
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
            },
            {
                "ingredient_id": 3,
                "name_kr": "판테놀",
                "product_ids": [101, 102],
                "presence_type": "all",
                "restrictions": [],
            },
            {
                "ingredient_id": 4,
                "name_kr": "나이아신아마이드",
                "product_ids": [101],
                "presence_type": "single",
                "restrictions": [],
            },
            {
                "ingredient_id": 5,
                "name_kr": "세라마이드",
                "product_ids": [102],
                "presence_type": "single",
                "restrictions": [],
            },
        ],
        "ingredient_ids": [1, 2, 3, 4, 5],
    }


def test_compare_four_products_classifies_all_partial_and_single(client: TestClient) -> None:
    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": [101, 102, 103, 104]},
    )

    assert response.status_code == 200
    presence_by_id = {
        item["ingredient_id"]: item for item in response.json()["ingredient_presence"]
    }
    assert presence_by_id[1]["presence_type"] == "all"
    assert presence_by_id[2]["presence_type"] == "partial"
    assert presence_by_id[2]["product_ids"] == [101, 102, 103]
    assert presence_by_id[3]["presence_type"] == "partial"
    assert presence_by_id[4]["presence_type"] == "single"
    assert response.json()["ingredient_ids"] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.parametrize(
    ("product_ids", "error_code"),
    [
        ([101, 101], "DUPLICATE_PRODUCT_IDS"),
        (
            [101, 102, 103, 104, 105],
            "PRODUCT_COMPARE_LIMIT_EXCEEDED",
        ),
    ],
)
def test_compare_products_rejects_invalid_product_sets(
    client: TestClient, product_ids: list[int], error_code: str
) -> None:
    response = client.post("/api/v1/products/compare", json={"product_ids": product_ids})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == error_code


def test_compare_products_fails_when_any_product_is_missing(client: TestClient) -> None:
    class MissingProductRepository(StubProductCompareRepository):
        async def get_products(self, product_ids: list[int]) -> list[ProductIngredientRows]:
            return [ProductIngredientRows(101, "제품 A", [1])]

    app.dependency_overrides[get_product_compare_repository] = lambda: MissingProductRepository()

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": [101, 999]},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_compare_products_fails_when_any_product_is_unanalyzable(client: TestClient) -> None:
    class UnanalyzableProductRepository(StubProductCompareRepository):
        async def get_products(self, product_ids: list[int]) -> list[ProductIngredientRows]:
            return [
                ProductIngredientRows(101, "제품 A", [1]),
                ProductIngredientRows(102, "제품 B", [None]),
            ]

    app.dependency_overrides[get_product_compare_repository] = lambda: (
        UnanalyzableProductRepository()
    )

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": [101, 102]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PRODUCT_NOT_ANALYZABLE"


def test_compare_products_requires_jwt(client: TestClient) -> None:
    app.dependency_overrides.pop(verify_jwt)

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": [101, 102]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_compare_products_requires_at_least_two_products(client: TestClient) -> None:
    product_ids = list(range(MIN_COMPARE_PRODUCT_COUNT - 1))
    response = client.post("/api/v1/products/compare", json={"product_ids": product_ids})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
