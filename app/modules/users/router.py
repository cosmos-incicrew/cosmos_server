from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import verify_jwt
from app.modules.users import service
from app.modules.users.repository import (
    UserProfileRepository,
    get_user_profile_repository,
)
from app.modules.users.schemas import ProfileResponse, ProfileUpsertRequest

router = APIRouter(prefix="/api/v1/users/me", tags=["users"])


@router.post("/profile", response_model=ProfileResponse)
async def upsert_profile(
    profile: ProfileUpsertRequest,
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[UserProfileRepository, Depends(get_user_profile_repository)],
) -> ProfileResponse:
    """온보딩 프로필을 저장한다. 이미 있으면 전체 덮어쓴다(수정 겸용)."""
    return await service.upsert_profile(repository, user_id, profile)


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[UserProfileRepository, Depends(get_user_profile_repository)],
) -> ProfileResponse:
    """마이페이지 진입 시 프로필을 조회한다. 온보딩 전이면 404."""
    try:
        return await service.get_profile(repository, user_id)
    except service.ProfileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PROFILE_NOT_FOUND", "message": "프로필이 아직 없습니다."},
        ) from None


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[UserProfileRepository, Depends(get_user_profile_repository)],
) -> None:
    """회원 탈퇴. 계정과 프로필을 지운다 (프로필은 FK cascade).

    지울 대상은 토큰의 sub 로만 정한다 — 요청 본문으로 받으면 남의 계정을 지울 수 있다.
    """
    await service.delete_account(repository, user_id)
