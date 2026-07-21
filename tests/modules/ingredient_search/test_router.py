from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.common.restrictions import RestrictionRow
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
                id=1,
                product_name="테스트 세럼",
                brand="테스트 브랜드",
                main_category="스킨케어",
                sub_category="에센스/세럼",
                detailed_category="세럼",
                product_url="https://example.com/products/1",
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

    async def get_product_ingredients(self, product_id: int) -> ProductIngredientRows | None:
        assert product_id == 1
        return ProductIngredientRows(
            id=1,
            product_name="테스트 세럼",
            ingredient_ids=[2700, 2247, 3851, None],
        )

    async def get_ingredient_names(self, ingredient_ids: list[int]) -> dict[int, str]:
        assert ingredient_ids == [2700, 2247, 3851]
        return {2700: "테스트 성분", 2247: "주의 성분", 3851: "일반 성분"}

    async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]:
        assert ingredient_ids == [2700, 2247, 3851]
        return [
            RestrictionRow(
                restriction_id=10,
                ingredient_id=2247,
                regulate_type="한도",
                provis_atrcl="사용 조건",
                limit_cond="배합 한도",
                is_registered_korea=True,
            )
        ]


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
                "id": 1,
                "product_name": "테스트 세럼",
                "brand": "테스트 브랜드",
                "main_category": "스킨케어",
                "sub_category": "에센스/세럼",
                "detailed_category": "세럼",
                "product_url": "https://example.com/products/1",
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


def test_get_product_ingredients_returns_ids_and_restricted_ingredients(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/products/1/ingredients")

    assert response.status_code == 200
    assert response.json() == {
        "id": 1,
        "product_name": "테스트 세럼",
        "ingredient_ids": [2700, 2247, 3851],
        "mapped_ingredient_count": 3,
        "unmapped_ingredient_count": 1,
        "restricted_ingredients": [
            {
                "ingredient_id": 2247,
                "name_kr": "주의 성분",
                "restrictions": [
                    {
                        "restriction_id": 10,
                        "regulate_type": "한도",
                        "provis_atrcl": "사용 조건",
                        "limit_cond": "배합 한도",
                        "is_registered_korea": True,
                    }
                ],
            }
        ],
    }


def test_get_product_ingredients_returns_empty_restrictions_when_none_exist(
    client: TestClient,
) -> None:
    class UnrestrictedProductRepository(FakeIngredientSearchRepository):
        async def get_restrictions(self, ingredient_ids: list[int]) -> list[RestrictionRow]:
            return []

    app.dependency_overrides[get_ingredient_search_repository] = lambda: (
        UnrestrictedProductRepository()
    )

    response = client.get("/api/v1/products/1/ingredients")

    assert response.status_code == 200
    assert response.json()["restricted_ingredients"] == []


def test_product_selection_flow_returns_ids_for_the_selected_candidate(client: TestClient) -> None:
    search_response = client.get("/api/v1/products/search", params={"q": "테스트 세럼"})
    product_id = search_response.json()["results"][0]["id"]
    ingredient_response = client.get(f"/api/v1/products/{product_id}/ingredients")

    assert search_response.status_code == 200
    assert ingredient_response.status_code == 200
    assert ingredient_response.json()["ingredient_ids"] == [2700, 2247, 3851]


def test_get_product_ingredient_ids_returns_not_found_for_unknown_product(
    client: TestClient,
) -> None:
    class MissingProductRepository(FakeIngredientSearchRepository):
        async def get_product_ingredients(self, product_id: int) -> ProductIngredientRows | None:
            return None

    app.dependency_overrides[get_ingredient_search_repository] = lambda: MissingProductRepository()

    response = client.get("/api/v1/products/999/ingredients")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_get_product_ingredient_ids_returns_unprocessable_for_unmapped_product(
    client: TestClient,
) -> None:
    class UnanalyzableProductRepository(FakeIngredientSearchRepository):
        async def get_product_ingredients(self, product_id: int) -> ProductIngredientRows | None:
            return ProductIngredientRows(
                id=product_id,
                product_name="미매핑 제품",
                ingredient_ids=[None],
            )

    app.dependency_overrides[get_ingredient_search_repository] = lambda: (
        UnanalyzableProductRepository()
    )

    response = client.get("/api/v1/products/2/ingredients")

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
