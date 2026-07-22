from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_jwt
from app.main import app
from app.modules.users.repository import (
    UserProfileRepository,
    get_user_profile_repository,
)
from app.modules.users.schemas import ProfileResponse, ProfileUpsertRequest

USER_ID = "11111111-2222-3333-4444-555555555555"


class FakeUserProfileRepository(UserProfileRepository):
    """user_id 로 격리된 인메모리 프로필 저장소."""

    def __init__(self) -> None:
        self.saved: dict[str, ProfileResponse] = {}
        self.deleted: list[str] = []

    async def get_profile(self, user_id: str) -> ProfileResponse | None:
        return self.saved.get(user_id)

    async def delete_account(self, user_id: str) -> None:
        self.deleted.append(user_id)
        self.saved.pop(user_id, None)

    async def upsert_profile(self, user_id: str, profile: ProfileUpsertRequest) -> ProfileResponse:
        # 실제 어댑터와 같은 규칙을 따른다 — 보내지 않은 필드는 기존 값을 남기고,
        # bsti_type 은 null 로 와도 덮지 않는다. 여기서 갈라지면 라우터 테스트가
        # 어댑터의 보존 로직을 지워도 초록 불을 준다.
        previous = self.saved.get(user_id)
        # 신규는 기존 값이 없으니 미전송 필드가 null 이 되는 게 맞다.
        merged = previous.model_dump() if previous else profile.model_dump()
        merged.update(profile.model_dump(exclude_unset=True))
        if merged.get("bsti_type") is None and previous is not None:
            merged["bsti_type"] = previous.bsti_type
        merged.pop("user_id", None)
        merged.pop("created_at", None)
        merged.pop("updated_at", None)
        saved = ProfileResponse(
            user_id=user_id,
            **merged,
            created_at="2026-07-20T00:00:00+00:00",
            updated_at="2026-07-20T00:00:00+00:00",
        )
        self.saved[user_id] = saved
        return saved


@pytest.fixture()
def repository() -> FakeUserProfileRepository:
    return FakeUserProfileRepository()


@pytest.fixture()
def client(repository: FakeUserProfileRepository) -> AsyncIterator[TestClient]:
    app.dependency_overrides[verify_jwt] = lambda: USER_ID
    app.dependency_overrides[get_user_profile_repository] = lambda: repository
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_upsert_then_get_returns_saved_profile(client: TestClient) -> None:
    payload = {
        "nickname": "민경",
        "age": 28,
        "gender": "female",
        "skin_concerns": ["pores", "acne", "pores"],
    }

    upsert_response = client.post("/api/v1/users/me/profile", json=payload)
    get_response = client.get("/api/v1/users/me/profile")

    assert upsert_response.status_code == 200
    # 중복 코드는 저장 시점에 제거된다.
    assert upsert_response.json()["skin_concerns"] == ["pores", "acne"]
    assert get_response.status_code == 200
    assert get_response.json() == upsert_response.json()


def test_profile_saves_without_nickname(client: TestClient) -> None:
    # 온보딩 화면이 닉네임을 강제하지 않는다 — 여기서 막으면 온보딩이 안 끝난다.
    response = client.post("/api/v1/users/me/profile", json={"age": 28})

    assert response.status_code == 200
    assert response.json()["nickname"] is None


def test_get_profile_before_onboarding_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/users/me/profile")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROFILE_NOT_FOUND"


def test_optional_fields_default_to_null(client: TestClient) -> None:
    response = client.post("/api/v1/users/me/profile", json={"nickname": "민경"})

    assert response.status_code == 200
    assert response.json()["age"] is None
    assert response.json()["gender"] is None
    assert response.json()["skin_concerns"] == []
    # 온보딩이 안 물었으면 false 가 아니라 null — 금기 검사가 "미수집"을 구분해야 한다.
    assert response.json()["is_pregnant"] is None
    assert response.json()["is_nursing"] is None


def test_nickname_edit_keeps_onboarding_fields_and_bsti(client: TestClient) -> None:
    """마이페이지에서 닉네임만 고치는 것은 나머지를 지우라는 뜻이 아니다.

    age·skin_concerns 는 추천 게이트(s1_context)라 지워지면 추천이 409 로 영구히
    막히고, bsti_type 은 검사 화면에서만 들어오므로 여기서 null 로 덮으면 사라진다.

    다만 이 테스트가 검증하는 것은 **API 계약**이지 어댑터 구현이 아니다 — 대역이
    보존 규칙을 자체 구현하므로, 실제 어댑터의 보존 로직을 지워도 여기서는 안 걸린다.
    그쪽은 test_repository.py 가 직접 잡는다.
    """
    client.post(
        "/api/v1/users/me/profile",
        json={"nickname": "민경", "age": 28, "skin_concerns": ["pores"], "bsti_type": "OSPW"},
    )

    # 앱의 _toJson 은 7개 필드를 **항상** 싣는다 — 검사 전이면 bsti_type 을 명시적
    # null 로 보낸다. 생략된 null 이 아니라 이 명시적 null 이 보존 대상이다.
    response = client.post(
        "/api/v1/users/me/profile",
        json={"nickname": "새이름", "age": 28, "skin_concerns": ["pores"], "bsti_type": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["nickname"] == "새이름"
    assert body["age"] == 28
    assert body["skin_concerns"] == ["pores"]
    assert body["bsti_type"] == "OSPW"


def test_pregnancy_flags_are_stored(client: TestClient) -> None:
    payload = {"nickname": "민경", "gender": "female", "is_pregnant": True, "is_nursing": False}

    response = client.post("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["is_pregnant"] is True
    assert response.json()["is_nursing"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"nickname": "민경", "skin_concerns": ["없는코드"]},
        {"nickname": "", "skin_concerns": []},
        {"nickname": "민경", "age": 999},
        {"nickname": "민경", "gender": "unknown"},
    ],
)
def test_invalid_payload_returns_422(client: TestClient, payload: dict) -> None:
    response = client.post("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_profile_endpoints_require_authentication() -> None:
    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False) as unauthenticated:
        response = unauthenticated.get("/api/v1/users/me/profile")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_delete_account_removes_only_token_user(
    client: TestClient, repository: FakeUserProfileRepository
) -> None:
    """지울 대상은 토큰의 sub 로만 정한다 — 남의 계정이 지워지면 안 된다."""
    other = "99999999-9999-9999-9999-999999999999"
    repository.saved[other] = ProfileResponse(
        user_id=other,
        nickname="남",
        age=30,
        gender="female",
        skin_concerns=[],
        is_pregnant=None,
        is_nursing=None,
        created_at=None,
        updated_at=None,
    )
    client.post("/api/v1/users/me/profile", json={"nickname": "민경", "age": 28})

    response = client.request("DELETE", "/api/v1/users/me")

    assert response.status_code == 204
    assert repository.deleted == [USER_ID]
    assert other in repository.saved


def test_get_profile_after_delete_returns_404(client: TestClient) -> None:
    client.post("/api/v1/users/me/profile", json={"nickname": "민경", "age": 28})

    client.request("DELETE", "/api/v1/users/me")

    assert client.get("/api/v1/users/me/profile").status_code == 404


def test_delete_account_requires_auth() -> None:
    """의존성 오버라이드 없이 = 토큰 없이 호출하면 401 이어야 한다."""
    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False) as anon:
        assert anon.request("DELETE", "/api/v1/users/me").status_code == 401
