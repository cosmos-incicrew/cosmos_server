from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from supabase import AsyncClient

from app.core import kakao
from app.core.config import Settings
from app.modules.users import repository as repository_module
from app.modules.users.repository import SupabaseUserProfileRepository
from app.modules.users.schemas import ProfileUpsertRequest

USER_ID = "11111111-2222-3333-4444-555555555555"
OTHER_USER_ID = "99999999-9999-9999-9999-999999999999"


@dataclass
class FakeResponse:
    data: Any


@dataclass
class FakeCalls:
    filters: list[tuple[str, Any]] = field(default_factory=list)
    upserts: list[tuple[dict[str, Any], str | None]] = field(default_factory=list)


class FakeQuery:
    def __init__(self, data: Any, calls: FakeCalls) -> None:
        self._data = data
        self._calls = calls

    def select(self, columns: str) -> "FakeQuery":
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._calls.filters.append((column, value))
        return self

    def upsert(self, payload: dict[str, Any], on_conflict: str | None = None) -> "FakeQuery":
        self._calls.upserts.append((payload, on_conflict))
        return self

    async def execute(self) -> FakeResponse:
        return FakeResponse(self._data)


@dataclass
class FakeIdentity:
    provider: str
    id: str


@dataclass
class FakeUser:
    user_metadata: dict[str, Any]
    app_metadata: dict[str, Any]
    identities: list[FakeIdentity]


@dataclass
class FakeUserResponse:
    user: FakeUser


class FakeAdmin:
    """auth.admin 대역 — 탈퇴 경로만 흉내낸다."""

    def __init__(
        self,
        provider: str,
        provider_id: str | None,
        identities: list["FakeIdentity"] | None = None,
        order: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._provider_id = provider_id
        self._identities = identities
        self.deleted: list[str] = []
        self.order = order if order is not None else []

    async def get_user_by_id(self, uid: str) -> FakeUserResponse:
        return FakeUserResponse(
            user=FakeUser(
                user_metadata={"provider_id": self._provider_id},
                app_metadata={"provider": self._provider},
                identities=(
                    self._identities
                    if self._identities is not None
                    else (
                        [FakeIdentity(self._provider, self._provider_id)]
                        if self._provider_id
                        else []
                    )
                ),
            )
        )

    async def delete_user(self, uid: str, should_soft_delete: bool = False) -> None:
        self.deleted.append(uid)
        self.order.append("delete")


class FakeAuth:
    def __init__(self, admin: FakeAdmin) -> None:
        self.admin = admin


class FakeSupabase:
    def __init__(
        self,
        rows: Any,
        provider: str = "google",
        provider_id: str | None = None,
        identities: list[FakeIdentity] | None = None,
    ) -> None:
        self._rows = rows
        self.calls = FakeCalls()
        self.order: list[str] = []
        self.admin = FakeAdmin(provider, provider_id, identities, self.order)
        self.auth = FakeAuth(self.admin)

    def table(self, table_name: str) -> FakeQuery:
        assert table_name == "user_profiles"
        return FakeQuery(self._rows, self.calls)


def _row(**overrides: Any) -> dict[str, Any]:
    """select가 전 컬럼을 명시하므로 실제 응답 행에는 항상 모든 키가 있다."""
    return {
        "user_id": USER_ID,
        "nickname": "민경",
        "age": 28,
        "gender": "female",
        "skin_concerns": ["pores"],
        "is_pregnant": None,
        "is_nursing": None,
        "bsti_type": None,
        "created_at": "2026-07-20T00:00:00+00:00",
        "updated_at": "2026-07-20T00:00:00+00:00",
        **overrides,
    }


def _settings(**overrides: Any) -> Settings:
    base = {
        "supabase_url": "http://localhost:54321",
        "supabase_service_role_key": "test",
        "gcp_project_id": "test-project",
        "langfuse_public_key": "test",
        "langfuse_secret_key": "test",
    }
    return Settings(**{**base, **overrides})


def _repository(
    rows: Any,
    provider: str = "google",
    provider_id: str | None = None,
    identities: list[FakeIdentity] | None = None,
    **settings_overrides: Any,
) -> tuple[SupabaseUserProfileRepository, FakeSupabase]:
    client = FakeSupabase(rows, provider, provider_id, identities)
    repository = SupabaseUserProfileRepository(
        cast(AsyncClient, client), _settings(**settings_overrides)
    )
    return repository, client


async def test_get_profile_filters_by_user_id() -> None:
    repository, client = _repository([_row()])

    profile = await repository.get_profile(USER_ID)

    # service_role 키는 RLS를 우회한다 — user_id 필터가 유일한 소유권 검사다.
    assert client.calls.filters == [("user_id", USER_ID)]
    assert profile is not None
    assert profile.user_id == USER_ID


async def test_get_profile_returns_none_before_onboarding() -> None:
    repository, _ = _repository([])

    assert await repository.get_profile(USER_ID) is None


async def test_get_profile_maps_null_skin_concerns_to_empty_list() -> None:
    repository, _ = _repository([_row(skin_concerns=None)])

    profile = await repository.get_profile(USER_ID)

    assert profile is not None
    assert profile.skin_concerns == []


async def test_upsert_profile_writes_token_user_id_not_request_body() -> None:
    repository, client = _repository([_row()])

    await repository.upsert_profile(
        USER_ID, ProfileUpsertRequest(nickname="민경", skin_concerns=["pores"])
    )

    payload, on_conflict = client.calls.upserts[0]
    assert payload["user_id"] == USER_ID
    assert payload["user_id"] != OTHER_USER_ID
    assert on_conflict == "user_id"
    # DB default는 갱신되지 않으므로 저장 시점마다 직접 넣어야 한다.
    assert payload["updated_at"]


async def test_upsert_profile_keeps_existing_bsti_when_not_sent() -> None:
    """BSTI는 검사 화면에서 따로 저장된다.

    프로필 저장은 전체 덮어쓰기라, bsti_type을 그대로 실어 보내면 마이페이지에서
    닉네임만 고쳐도 검사 결과가 null로 지워진다. 컬럼 자체를 payload에서 빼야 한다.
    """
    repository, client = _repository([_row()])

    await repository.upsert_profile(USER_ID, ProfileUpsertRequest(nickname="민경"))

    payload, _ = client.calls.upserts[0]
    assert "bsti_type" not in payload


async def test_upsert_profile_keeps_bsti_when_client_sends_explicit_null() -> None:
    """앱은 검사 전이면 bsti_type 을 **명시적 null** 로 싣는다 (_toJson 은 7필드 고정).

    exclude_unset 은 "보내지 않은" 필드만 걸러내므로 이 null 은 그대로 통과한다.
    payload 에서 컬럼을 빼는 pop 이 유일한 방어선이다 — 없으면 마이페이지에서
    닉네임만 고쳐도 BSTI 검사 결과가 지워진다.
    """
    repository, client = _repository([_row()])

    await repository.upsert_profile(
        USER_ID, ProfileUpsertRequest(nickname="새이름", bsti_type=None)
    )

    payload, _ = client.calls.upserts[0]
    assert "bsti_type" not in payload


async def test_upsert_profile_omits_fields_the_client_did_not_send() -> None:
    """보내지 않은 필드는 payload 에서 빠져야 기존 값이 남는다.

    upsert 는 전체 덮어쓰기라, 빠진 필드를 null 로 채워 보내면 닉네임만 담은 요청
    하나가 age·skin_concerns 를 지운다. 그 둘은 추천 게이트(s1_context)여서,
    지워지면 추천이 409 PROFILE_ONBOARDING_REQUIRED 로 영구히 막힌다.
    """
    repository, client = _repository([_row()])

    await repository.upsert_profile(USER_ID, ProfileUpsertRequest(nickname="새이름"))

    payload, _ = client.calls.upserts[0]
    assert payload["nickname"] == "새이름"
    assert "age" not in payload
    assert "skin_concerns" not in payload


async def test_upsert_profile_writes_explicit_nulls() -> None:
    """명시적으로 보낸 null 은 지우려는 의도이므로 그대로 반영한다.

    지금 앱은 항상 전 필드를 실어 보내므로, exclude_unset 이 이 경로를 막으면
    성별을 남성으로 바꿔 임신 여부를 비우는 동작이 조용히 깨진다.
    """
    repository, client = _repository([_row()])

    await repository.upsert_profile(
        USER_ID,
        ProfileUpsertRequest(nickname="민경", age=28, gender="male", is_pregnant=None),
    )

    payload, _ = client.calls.upserts[0]
    assert payload["age"] == 28
    assert "is_pregnant" in payload and payload["is_pregnant"] is None


async def test_upsert_profile_writes_bsti_when_sent() -> None:
    repository, client = _repository([_row(bsti_type="OSPW")])

    await repository.upsert_profile(
        USER_ID, ProfileUpsertRequest(nickname="민경", bsti_type="OSPW")
    )

    payload, _ = client.calls.upserts[0]
    assert payload["bsti_type"] == "OSPW"


async def test_upsert_profile_rejects_malformed_bsti_code() -> None:
    """오타·소문자가 DB까지 가면 제약에 걸려 500이 된다 — 요청 단계에서 막는다."""
    for bad in ("ospw", "OSP", "XSPW", "OSPWW"):
        with pytest.raises(ValueError):
            ProfileUpsertRequest(bsti_type=bad)


async def test_upsert_profile_raises_when_nothing_saved() -> None:
    repository, _ = _repository([])

    with pytest.raises(RuntimeError):
        await repository.upsert_profile(USER_ID, ProfileUpsertRequest(nickname="민경"))


class _UnlinkSpy:
    """kakao.unlink 대역. 호출된 회원번호를 기록한다."""

    def __init__(self, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.called_with: list[str] = []

    order: list[str] | None = None

    async def __call__(self, settings: Settings, kakao_user_id: str) -> bool:
        self.called_with.append(kakao_user_id)
        if self.order is not None:
            self.order.append("unlink")
        return self.succeeds


async def test_delete_account_unlinks_kakao(monkeypatch: pytest.MonkeyPatch) -> None:
    """탈퇴하면 카카오 앱 연결까지 끊는다 — 안 끊으면 사용자의 카카오계정에
    서비스가 그대로 남고, 재로그인 시 동의 화면도 안 뜬다."""
    spy = _UnlinkSpy()
    monkeypatch.setattr(kakao, "unlink", spy)
    monkeypatch.setattr(repository_module.kakao, "unlink", spy)
    repository, client = _repository([], provider="kakao", provider_id="5000925099")

    await repository.delete_account(USER_ID)

    assert client.admin.deleted == [USER_ID]
    assert spy.called_with == ["5000925099"]


async def test_delete_account_skips_unlink_for_non_kakao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구글 사용자에게 카카오 연결 해제를 호출하면 안 된다."""
    spy = _UnlinkSpy()
    monkeypatch.setattr(repository_module.kakao, "unlink", spy)
    repository, client = _repository([], provider="google", provider_id="ignored")

    await repository.delete_account(USER_ID)

    assert client.admin.deleted == [USER_ID]
    assert spy.called_with == []


async def test_delete_account_succeeds_even_if_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카카오가 응답하지 않아도 사용자가 요청한 탈퇴 자체는 끝나야 한다."""
    spy = _UnlinkSpy(succeeds=False)
    monkeypatch.setattr(repository_module.kakao, "unlink", spy)
    repository, client = _repository([], provider="kakao", provider_id="5000925099")

    await repository.delete_account(USER_ID)

    assert client.admin.deleted == [USER_ID]


async def test_delete_account_unlinks_secondary_kakao_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구글로 가입한 뒤 카카오를 추가 연결한 사용자도 연결을 끊어야 한다.

    app_metadata["provider"]는 최초 제공자만 담아 "google"로 남는다.
    """
    spy = _UnlinkSpy()
    monkeypatch.setattr(repository_module.kakao, "unlink", spy)
    repository, _ = _repository(
        [],
        provider="google",
        identities=[
            FakeIdentity("google", "117956512897529250776"),
            FakeIdentity("kakao", "5000925099"),
        ],
    )

    await repository.delete_account(USER_ID)

    assert spy.called_with == ["5000925099"]


async def test_delete_account_reads_id_before_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """순서가 뒤집히면 회원번호를 읽을 계정이 이미 없어 연결 해제가 조용히 빠진다."""
    spy = _UnlinkSpy()
    repository, client = _repository([], provider="kakao", provider_id="5000925099")
    spy.order = client.order
    monkeypatch.setattr(repository_module.kakao, "unlink", spy)

    await repository.delete_account(USER_ID)

    assert client.order == ["delete", "unlink"]
