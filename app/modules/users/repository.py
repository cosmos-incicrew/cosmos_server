"""users 모듈의 Supabase 조회·저장 어댑터."""

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from supabase import AsyncClient

from app.core import kakao
from app.core.config import Settings, get_settings
from app.core.supabase import get_supabase, rows
from app.modules.users.schemas import ProfileResponse, ProfileUpsertRequest

logger = logging.getLogger(__name__)

_KAKAO_PROVIDER = "kakao"

_PROFILE_COLUMNS = (
    "user_id,nickname,age,gender,skin_concerns,is_pregnant,is_nursing,"
    "bsti_type,created_at,updated_at"
)


class UserProfileRepository(Protocol):
    async def get_profile(self, user_id: str) -> ProfileResponse | None: ...

    async def upsert_profile(
        self, user_id: str, profile: ProfileUpsertRequest
    ) -> ProfileResponse: ...

    async def delete_account(self, user_id: str) -> None: ...


class SupabaseUserProfileRepository:
    def __init__(self, client: AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def get_profile(self, user_id: str) -> ProfileResponse | None:
        response = await (
            self._client.table("user_profiles")
            .select(_PROFILE_COLUMNS)
            .eq("user_id", user_id)
            .execute()
        )
        found = rows(response)
        return _to_profile(found[0]) if found else None

    async def upsert_profile(self, user_id: str, profile: ProfileUpsertRequest) -> ProfileResponse:
        payload = {
            "user_id": user_id,
            # upsert 는 전체 덮어쓰기다. exclude_unset 이 없으면 바디에 **없던** 필드까지
            # null 로 실려 나가, 닉네임만 담은 요청 하나가 age·skin_concerns 를 지운다.
            # 그 둘은 추천 게이트(s1_context)라, 지워지면 추천이 409 로 영구히 막힌다.
            # 지금 앱은 항상 전 필드를 보내므로 동작은 그대로다 — 함정만 없앤다.
            **profile.model_dump(exclude_unset=True),
            # created_at 과 달리 updated_at 은 DB default 가 갱신을 안 해주므로 직접 넣는다.
            "updated_at": datetime.now(UTC).isoformat(),
        }
        # BSTI 는 검사 화면에서 따로 저장된다. 프로필 저장에 안 실려 오면 컬럼을 아예
        # 빼서 기존 값을 남긴다 — 넣은 채로 두면 닉네임만 고쳐도 BSTI 가 지워진다.
        if payload.get("bsti_type") is None:
            payload.pop("bsti_type", None)

        response = await (
            self._client.table("user_profiles").upsert(payload, on_conflict="user_id").execute()
        )
        saved = rows(response)
        if not saved:
            raise RuntimeError("프로필 저장 결과가 비어 있습니다.")
        return _to_profile(saved[0])

    async def delete_account(self, user_id: str) -> None:
        """계정을 지우고, 카카오 사용자면 앱 연결까지 끊는다.

        user_profiles 는 FK 의 on delete cascade 로 함께 지워진다
        (migration 009_add_user_profiles_user_fk).

        순서: 카카오 회원번호는 계정 안에 있으므로 **지우기 전에** 읽고, 연결 해제는
        삭제 **뒤에** 시도한다 — 카카오 장애가 탈퇴 자체를 막으면 안 된다.
        """
        kakao_user_id = await self._kakao_user_id(user_id)
        await self._client.auth.admin.delete_user(user_id)
        if kakao_user_id:
            await kakao.unlink(self._settings, kakao_user_id)

    async def _kakao_user_id(self, user_id: str) -> str | None:
        """카카오 연결이 있으면 카카오 회원번호를, 없으면 None.

        app_metadata["provider"]는 **최초** 제공자만 담는다. 구글로 가입한 뒤
        카카오를 추가 연결한 사용자는 그 값이 "google"이라 연결 해제를 건너뛰게 된다.
        identities를 직접 훑으면 1차·2차를 가리지 않는다.
        """
        try:
            response = await self._client.auth.admin.get_user_by_id(user_id)
            for identity in response.user.identities or []:
                if identity.provider == _KAKAO_PROVIDER:
                    return str(identity.id)
            return None
        except Exception:
            # 조회에 실패해도 탈퇴는 진행한다 — 연결 해제만 생략된다.
            logger.warning("카카오 회원번호 조회 실패 user_id=%s", user_id, exc_info=True)
            return None


async def get_user_profile_repository() -> UserProfileRepository:
    return SupabaseUserProfileRepository(await get_supabase(), get_settings())


def _to_profile(row: dict[str, Any]) -> ProfileResponse:
    return ProfileResponse.model_validate({**row, "skin_concerns": row.get("skin_concerns") or []})
