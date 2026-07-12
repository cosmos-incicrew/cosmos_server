from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.ingredient_search.repository import (
    IngredientSearchRepository,
    ProductIngredientRows,
    get_ingredient_search_repository,
)
from app.modules.ingredient_search.schemas import (
    IngredientSearchCandidate,
    ProductSearchCandidate,
)


class FakeIngredientSearchRepository(IngredientSearchRepository):
    async def search_products(self, query: str, limit: int) -> list[ProductSearchCandidate]:
        assert query == "테스트 세럼"
        assert limit == 20
        return [
            ProductSearchCandidate(
                product_id="product-001",
                product_name="테스트 세럼",
                main_category="스킨케어",
                sub_category="에센스/세럼",
            )
        ]

    async def search_ingredients(self, query: str, limit: int) -> list[IngredientSearchCandidate]:
        assert query == "테스트 이명"
        assert limit == 20
        return [
            IngredientSearchCandidate(
                ingredient_id=2700,
                name_kr="테스트 성분",
                name_en="Test Ingredient",
            )
        ]

    async def get_product_ingredients(self, product_id: str) -> ProductIngredientRows | None:
        assert product_id == "product-001"
        return ProductIngredientRows(
            product_id="product-001",
            product_name="테스트 세럼",
            ingredient_ids=[2700, 2247, 3851, None],
        )


@pytest.fixture()
def client() -> AsyncIterator[TestClient]:
    app.dependency_overrides[verify_jwt] = lambda: "user-123"
    app.dependency_overrides[get_ingredient_search_repository] = lambda: (
        FakeIngredientSearchRepository()
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_search_products_returns_analyzable_candidates(client: TestClient) -> None:
    response = client.get("/api/v1/products/search", params={"q": "테스트 세럼"})

    assert response.status_code == 200
    assert response.json() == {
        "query": "테스트 세럼",
        "results": [
            {
                "product_id": "product-001",
                "product_name": "테스트 세럼",
                "main_category": "스킨케어",
                "sub_category": "에센스/세럼",
            }
        ],
    }


def test_search_ingredients_returns_alias_candidates(client: TestClient) -> None:
    response = client.get("/api/v1/ingredients/search", params={"q": "테스트 이명"})

    assert response.status_code == 200
    assert response.json() == {
        "query": "테스트 이명",
        "results": [
            {
                "ingredient_id": 2700,
                "name_kr": "테스트 성분",
                "name_en": "Test Ingredient",
            }
        ],
    }


def test_get_product_ingredient_ids_returns_resolved_integer_ids(client: TestClient) -> None:
    response = client.get("/api/v1/products/product-001/ingredients")

    assert response.status_code == 200
    assert response.json() == {
        "product_id": "product-001",
        "product_name": "테스트 세럼",
        "ingredient_ids": [2700, 2247, 3851],
        "mapped_ingredient_count": 3,
        "unmapped_ingredient_count": 1,
    }


def test_product_selection_flow_returns_ids_for_the_selected_candidate(client: TestClient) -> None:
    search_response = client.get("/api/v1/products/search", params={"q": "테스트 세럼"})
    product_id = search_response.json()["results"][0]["product_id"]
    ingredient_response = client.get(f"/api/v1/products/{product_id}/ingredients")

    assert search_response.status_code == 200
    assert ingredient_response.status_code == 200
    assert ingredient_response.json()["ingredient_ids"] == [2700, 2247, 3851]


def test_get_product_ingredient_ids_returns_not_found_for_unknown_product(
    client: TestClient,
) -> None:
    class MissingProductRepository(FakeIngredientSearchRepository):
        async def get_product_ingredients(self, product_id: str) -> ProductIngredientRows | None:
            return None

    app.dependency_overrides[get_ingredient_search_repository] = lambda: MissingProductRepository()

    response = client.get("/api/v1/products/missing/ingredients")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_get_product_ingredient_ids_returns_unprocessable_for_unmapped_product(
    client: TestClient,
) -> None:
    class UnanalyzableProductRepository(FakeIngredientSearchRepository):
        async def get_product_ingredients(self, product_id: str) -> ProductIngredientRows | None:
            return ProductIngredientRows(
                product_id=product_id,
                product_name="미매핑 제품",
                ingredient_ids=[None],
            )

    app.dependency_overrides[get_ingredient_search_repository] = lambda: (
        UnanalyzableProductRepository()
    )

    response = client.get("/api/v1/products/product-unmapped/ingredients")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PRODUCT_NOT_ANALYZABLE"


@pytest.mark.parametrize(
    "params",
    [
        {"q": ""},
        {"q": "테스트 세럼", "limit": 51},
    ],
)
def test_search_products_rejects_invalid_query_parameters(
    client: TestClient, params: dict[str, int | str]
) -> None:
    response = client.get("/api/v1/products/search", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_products_requires_jwt(client: TestClient) -> None:
    app.dependency_overrides.pop(verify_jwt)

    response = client.get("/api/v1/products/search", params={"q": "테스트 세럼"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"
