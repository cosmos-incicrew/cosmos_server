"""ingredient_detail router 테스트. 엔드포인트가 service를 호출해 응답을 반환하는지."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.ingredient_detail import router as detail_router
from app.modules.ingredient_detail.schemas import (
    IngredientDetailResponse,
    ProductSummaryResponse,
    TopIngredient,
)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # 테스트에서는 실제 JWT 검증을 우회한다(가짜 user_id 주입).
    app.dependency_overrides[verify_jwt] = lambda: "user-123"
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _fake_detail_ok(ingredient_id: int) -> IngredientDetailResponse:
    return IngredientDetailResponse(
        status="ok",
        ingredient_id=ingredient_id,
        name="테스트성분",
        body="해설 본문",
        safety="자극 낮음",
        reference_source="PubChem",
    )


def test_detail_endpoint_returns_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detail_router.service, "get_ingredient_detail", _fake_detail_ok)

    response = client.get("/api/v1/ingredients/1/detail")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ingredient_id"] == 1
    assert body["name"] == "테스트성분"


def test_unconfirmed_returns_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_unknown(ingredient_id: int) -> IngredientDetailResponse:
        return IngredientDetailResponse(
            status="확인 불가", ingredient_id=ingredient_id, reason="성분 근거 없음"
        )

    monkeypatch.setattr(detail_router.service, "get_ingredient_detail", _fake_unknown)

    response = client.get("/api/v1/ingredients/999/detail")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "확인 불가"
    assert body["body"] is None


def test_product_summary_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_summary(ingredient_ids: list[int]) -> ProductSummaryResponse:
        return ProductSummaryResponse(
            status="ok",
            top_ingredients=[TopIngredient(ingredient_id=1, name="성분A")],
            summary="제품 요약입니다.",
        )

    monkeypatch.setattr(detail_router.service, "get_product_summary", _fake_summary)

    response = client.post(
        "/api/v1/ingredients/product-summary", json={"ingredient_ids": [1, 2, 3]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["summary"] == "제품 요약입니다."


def test_detail_requires_jwt(client: TestClient) -> None:
    app.dependency_overrides.pop(verify_jwt)

    response = client.get("/api/v1/ingredients/1/detail")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_product_summary_requires_jwt(client: TestClient) -> None:
    """제품 요약 엔드포인트도 인증이 필요하다."""
    app.dependency_overrides.pop(verify_jwt)

    response = client.post("/api/v1/ingredients/product-summary", json={"ingredient_ids": [1]})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_product_summary_rejects_missing_body(client: TestClient) -> None:
    """ingredient_ids가 없으면 검증 실패(422)."""
    response = client.post("/api/v1/ingredients/product-summary", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_product_summary_rejects_non_integer_ids(client: TestClient) -> None:
    """ingredient_ids에 정수가 아닌 값이 오면 검증 실패(422)."""
    response = client.post("/api/v1/ingredients/product-summary", json={"ingredient_ids": ["abc"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_detail_rejects_non_integer_path(client: TestClient) -> None:
    """경로의 ingredient_id가 정수가 아니면 검증 실패(422)."""
    response = client.get("/api/v1/ingredients/abc/detail")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_product_summary_unconfirmed_returns_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """제품 요약이 '확인 불가'여도 200으로 반환한다(에러가 아님)."""

    async def _fake_unknown(ingredient_ids: list[int]) -> ProductSummaryResponse:
        return ProductSummaryResponse(status="확인 불가", reason="제품 성분 근거 없음")

    monkeypatch.setattr(detail_router.service, "get_product_summary", _fake_unknown)

    response = client.post("/api/v1/ingredients/product-summary", json={"ingredient_ids": [999]})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "확인 불가"
    assert body["summary"] is None
    assert body["top_ingredients"] == []
