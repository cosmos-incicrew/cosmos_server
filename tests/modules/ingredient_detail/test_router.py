"""ingredient_detail router 테스트. 엔드포인트가 service를 호출해 응답을 반환하는지."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.ingredient_detail import router as detail_router
from app.modules.ingredient_detail.schemas import IngredientDetailResponse

client = TestClient(app, raise_server_exceptions=False)


async def _fake_detail_ok(ingredient_id: int) -> IngredientDetailResponse:
    return IngredientDetailResponse(
        status="ok",
        ingredient_id=ingredient_id,
        name="테스트성분",
        body="해설 본문",
        safety="자극 낮음",
        reference_source="PubChem",
    )


def test_detail_endpoint_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detail_router.service, "get_ingredient_detail", _fake_detail_ok)

    response = client.get("/api/v1/ingredients/1/detail")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ingredient_id"] == 1
    assert body["name"] == "테스트성분"


def test_unconfirmed_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_product_summary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_summary(ingredient_ids: list[int]):
        from app.modules.ingredient_detail.schemas import (
            ProductSummaryResponse,
            TopIngredient,
        )

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
