from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import verify_jwt
from app.modules.recommendations import service
from app.modules.recommendations.schemas import RecommendationResponse

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.post("", response_model=RecommendationResponse)
async def create_recommendations(
    user_id: Annotated[str, Depends(verify_jwt)],
) -> RecommendationResponse:
    """성분 추천 — 컨텍스트 조립 → retrieval → 안전성 필터 → 생성. 요청 바디 없음."""
    return await service.create_recommendations(user_id)
