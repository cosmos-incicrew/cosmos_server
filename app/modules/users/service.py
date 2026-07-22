"""프로필 조회·저장 로직. HTTP 관심사는 router.py가 맡는다."""

from app.modules.users.repository import UserProfileRepository
from app.modules.users.schemas import ProfileResponse, ProfileUpsertRequest


class ProfileNotFoundError(Exception):
    """온보딩을 아직 하지 않은 사용자."""


async def get_profile(repository: UserProfileRepository, user_id: str) -> ProfileResponse:
    profile = await repository.get_profile(user_id)
    if profile is None:
        raise ProfileNotFoundError(user_id)
    return profile


async def upsert_profile(
    repository: UserProfileRepository, user_id: str, profile: ProfileUpsertRequest
) -> ProfileResponse:
    return await repository.upsert_profile(user_id, profile)


async def delete_account(repository: UserProfileRepository, user_id: str) -> None:
    """회원 탈퇴. 토큰에서 꺼낸 user_id 만 쓴다 — 남의 계정은 지울 수 없다."""
    await repository.delete_account(user_id)
