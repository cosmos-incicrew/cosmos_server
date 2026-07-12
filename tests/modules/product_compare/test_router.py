from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.product_compare.repository import (
    ProductCompareRepository,
    ProductIngredientRows,
    RestrictionRow,
    get_product_compare_repository,
)


class FakeProductCompareRepository(ProductCompareRepository):
    async def get_products(self, product_ids: list[str]) -> list[ProductIngredientRows]:
        assert product_ids == ["product-a", "product-b"]
        return [
            ProductIngredientRows(
                product_id="product-a",
                product_name="제품 A",
                ingredient_ids=[1, 2, 3],
            ),
            ProductIngredientRows(
                product_id="product-b",
                product_name="제품 B",
                ingredient_ids=[1, 2, 4],
            ),
        ]

    async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]:
        assert ingredient_ids == [1, 2, 3, 4]
        return {1: "정제수", 2: "글리세린", 3: "판테놀", 4: "나이아신아마이드"}

    async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]:
        assert ingredient_ids == [1, 2, 3, 4]
        return [
            RestrictionRow(
                restriction_id=10,
                ingredient_id=2,
                regulate_type="한도",
                provis_atrcl="사용 조건",
                limit_cond="배합 한도",
                is_registered_korea=True,
            )
        ]


@pytest.fixture()
def client() -> AsyncIterator[TestClient]:
    app.dependency_overrides[verify_jwt] = lambda: "user-123"
    app.dependency_overrides[get_product_compare_repository] = lambda: (
        FakeProductCompareRepository()
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_compare_products_returns_presence_and_resolved_ids(client: TestClient) -> None:
    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": ["product-a", "product-b"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "products": [
            {"product_id": "product-a", "product_name": "제품 A"},
            {"product_id": "product-b", "product_name": "제품 B"},
        ],
        "ingredient_presence": [
            {
                "ingredient_id": 1,
                "name_kr": "정제수",
                "product_ids": ["product-a", "product-b"],
                "presence_type": "all",
                "restrictions": [],
            },
            {
                "ingredient_id": 2,
                "name_kr": "글리세린",
                "product_ids": ["product-a", "product-b"],
                "presence_type": "all",
                "restrictions": [
                    {
                        "restriction_id": 10,
                        "regulate_type": "한도",
                        "provis_atrcl": "사용 조건",
                        "limit_cond": "배합 한도",
                        "is_registered_korea": True,
                    }
                ],
            },
            {
                "ingredient_id": 3,
                "name_kr": "판테놀",
                "product_ids": ["product-a"],
                "presence_type": "single",
                "restrictions": [],
            },
            {
                "ingredient_id": 4,
                "name_kr": "나이아신아마이드",
                "product_ids": ["product-b"],
                "presence_type": "single",
                "restrictions": [],
            },
        ],
        "ingredient_ids": [1, 2, 3, 4],
    }


def test_compare_four_products_classifies_all_partial_and_single(client: TestClient) -> None:
    class FourProductRepository(FakeProductCompareRepository):
        async def get_products(self, product_ids: list[str]) -> list[ProductIngredientRows]:
            assert product_ids == ["product-a", "product-b", "product-c", "product-d"]
            return [
                ProductIngredientRows("product-a", "제품 A", [1, 2, 3, 4]),
                ProductIngredientRows("product-b", "제품 B", [1, 2, 3, 5]),
                ProductIngredientRows("product-c", "제품 C", [1, 2, 6]),
                ProductIngredientRows("product-d", "제품 D", [1, 7]),
            ]

        async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]:
            return {ingredient_id: f"성분 {ingredient_id}" for ingredient_id in ingredient_ids}

        async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]:
            return []

    app.dependency_overrides[get_product_compare_repository] = lambda: FourProductRepository()

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": ["product-a", "product-b", "product-c", "product-d"]},
    )

    assert response.status_code == 200
    presence_by_id = {
        item["ingredient_id"]: item for item in response.json()["ingredient_presence"]
    }
    assert presence_by_id[1]["presence_type"] == "all"
    assert presence_by_id[2]["presence_type"] == "partial"
    assert presence_by_id[2]["product_ids"] == ["product-a", "product-b", "product-c"]
    assert presence_by_id[3]["presence_type"] == "partial"
    assert presence_by_id[4]["presence_type"] == "single"
    assert response.json()["ingredient_ids"] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.parametrize(
    ("product_ids", "error_code"),
    [
        (["product-a", "product-a"], "DUPLICATE_PRODUCT_IDS"),
        (
            ["product-a", "product-b", "product-c", "product-d", "product-e"],
            "PRODUCT_COMPARE_LIMIT_EXCEEDED",
        ),
    ],
)
def test_compare_products_rejects_invalid_product_sets(
    client: TestClient, product_ids: list[str], error_code: str
) -> None:
    response = client.post("/api/v1/products/compare", json={"product_ids": product_ids})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == error_code


def test_compare_products_fails_when_any_product_is_missing(client: TestClient) -> None:
    class MissingProductRepository(FakeProductCompareRepository):
        async def get_products(self, product_ids: list[str]) -> list[ProductIngredientRows]:
            return [ProductIngredientRows("product-a", "제품 A", [1])]

    app.dependency_overrides[get_product_compare_repository] = lambda: MissingProductRepository()

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": ["product-a", "missing"]},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_compare_products_fails_when_any_product_is_unanalyzable(client: TestClient) -> None:
    class UnanalyzableProductRepository(FakeProductCompareRepository):
        async def get_products(self, product_ids: list[str]) -> list[ProductIngredientRows]:
            return [
                ProductIngredientRows("product-a", "제품 A", [1]),
                ProductIngredientRows("product-b", "제품 B", [None]),
            ]

    app.dependency_overrides[get_product_compare_repository] = lambda: (
        UnanalyzableProductRepository()
    )

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": ["product-a", "product-b"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PRODUCT_NOT_ANALYZABLE"


def test_compare_products_requires_jwt(client: TestClient) -> None:
    app.dependency_overrides.pop(verify_jwt)

    response = client.post(
        "/api/v1/products/compare",
        json={"product_ids": ["product-a", "product-b"]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"
