"""recommendations router 통합 테스트 — 200 정상 / 200 확인불가 / 401 / 409 / 429 / 502 / 503."""

from collections.abc import Iterator

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.recommendations import errors, rate_limit
from app.modules.recommendations import router as rec_router
from app.modules.recommendations.constants import DISCLAIMER, RATE_LIMIT_MAX
from app.modules.recommendations.schemas import (
    Advisory,
    Answer,
    RecommendationResponse,
    UserProfile,
)

_PATH = "/api/v1/recommendations"


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> Iterator[None]:
    """rate limit 상태는 모듈 전역이라 테스트 간 누적을 막는다."""
    rate_limit._reset()
    yield
    rate_limit._reset()


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
            answer=Answer(cause_analysis="원인", recommendation="추천", usage_guide="사용법"),
            user_profile=UserProfile(age=32, concerns=["pores"]),
            disclaimer=DISCLAIMER,
        )

    monkeypatch.setattr(rec_router.service, "create_recommendations", _ok)

    response = client.post(_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["answer"]
    assert "recommended_ingredients" not in body


def test_insufficient_evidence_is_200_not_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _insufficient(user_id: str) -> RecommendationResponse:
        return RecommendationResponse(
            status="insufficient_evidence",
            advisory=Advisory(
                code="no_evidence", message="근거를 찾지 못했습니다.", action="take_bsti"
            ),
            user_profile=UserProfile(age=32, concerns=["sensitivity"]),
            disclaimer=DISCLAIMER,
        )

    monkeypatch.setattr(rec_router.service, "create_recommendations", _insufficient)

    response = client.post(_PATH)

    # 확인 불가는 에러가 아니라 정형 응답이다 (01 §3)
    assert response.status_code == 200
    assert response.json()["advisory"]["action"] == "take_bsti"
    assert response.json()["answer"] is None


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


def _mock_service(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(rec_router.service, "create_recommendations", fn)


def test_rate_limit_returns_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """윈도우 내 상한을 넘긴 (RATE_LIMIT_MAX+1)번째 호출은 429 로 막힌다."""

    async def _ok(user_id: str) -> RecommendationResponse:
        return RecommendationResponse(
            status="ok", user_profile=UserProfile(), disclaimer=DISCLAIMER
        )

    _mock_service(monkeypatch, _ok)

    for _ in range(RATE_LIMIT_MAX):
        assert client.post(_PATH).status_code == 200
    blocked = client.post(_PATH)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"


def test_llm_upstream_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise(user_id: str) -> RecommendationResponse:
        raise errors.llm_upstream_error()

    _mock_service(monkeypatch, _raise)

    response = client.post(_PATH)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "LLM_UPSTREAM_ERROR"


def test_db_unavailable_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise(user_id: str) -> RecommendationResponse:
        raise errors.db_unavailable()

    _mock_service(monkeypatch, _raise)

    response = client.post(_PATH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DB_UNAVAILABLE"
