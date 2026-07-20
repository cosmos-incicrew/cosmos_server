"""recommendations router 통합 테스트 — 200 정상 / 200 확인불가 / 401 / 409."""

from collections.abc import Iterator

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.recommendations import router as rec_router
from app.modules.recommendations.constants import DISCLAIMER
from app.modules.recommendations.schemas import (
    ContextUsed,
    RecommendationResponse,
    RecommendedIngredient,
)

_PATH = "/api/v1/recommendations"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app.dependency_overrides[verify_jwt] = lambda: "user-123"
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_returns_recommendations(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(user_id: str) -> RecommendationResponse:
        return RecommendationResponse(
            status="ok",
            recommended_ingredients=[
                RecommendedIngredient(
                    ingredient_id=1,
                    name_kor="나이아신아마이드",
                    reason="모공 관리에 도움이 됩니다.",
                )
            ],
            context_used=ContextUsed(age=32, concerns=["pores"]),
            disclaimer=DISCLAIMER,
        )

    monkeypatch.setattr(rec_router.service, "create_recommendations", _ok)

    response = client.post(_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["recommended_ingredients"][0]["name_kor"] == "나이아신아마이드"
    assert body["recommended_products"] == []


def test_insufficient_evidence_is_200_not_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _insufficient(user_id: str) -> RecommendationResponse:
        return RecommendationResponse(
            status="insufficient_evidence",
            message="근거를 찾지 못했습니다.",
            suggested_action="take_bsti",
            context_used=ContextUsed(age=32, concerns=["sensitivity"]),
            disclaimer=DISCLAIMER,
        )

    monkeypatch.setattr(rec_router.service, "create_recommendations", _insufficient)

    response = client.post(_PATH)

    # 확인 불가는 에러가 아니라 정형 응답이다 (01 §3)
    assert response.status_code == 200
    assert response.json()["suggested_action"] == "take_bsti"
    assert response.json()["recommended_ingredients"] == []


def test_onboarding_incomplete_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _needs_onboarding(user_id: str) -> RecommendationResponse:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROFILE_ONBOARDING_REQUIRED",
                "message": "추천을 받으려면 먼저 나이와 피부 고민을 입력해 주세요.",
            },
        )

    monkeypatch.setattr(rec_router.service, "create_recommendations", _needs_onboarding)

    response = client.post(_PATH)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROFILE_ONBOARDING_REQUIRED"


def test_requires_jwt(client: TestClient) -> None:
    app.dependency_overrides.pop(verify_jwt)

    response = client.post(_PATH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"
