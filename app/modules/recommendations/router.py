from typing import Annotated

from fastapi import APIRouter, Depends

from app.modules.recommendations import service
from app.modules.recommendations.rate_limit import rate_limited_user
from app.modules.recommendations.schemas import RecommendationResponse

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.post("", response_model=RecommendationResponse)
async def create_recommendations(
    user_id: Annotated[str, Depends(rate_limited_user)],
) -> RecommendationResponse:
    """성분 추천 — 컨텍스트 조립 → retrieval → 안전성 필터 → 생성. 요청 바디 없음.

    `rate_limited_user` 가 JWT 검증(verify_jwt) 후 호출 빈도 상한까지 적용한다.
    """
    return await service.create_recommendations(user_id)
