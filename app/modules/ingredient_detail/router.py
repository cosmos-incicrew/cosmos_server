from fastapi import APIRouter

from app.modules.ingredient_detail import service
from app.modules.ingredient_detail.schemas import IngredientDetailResponse

router = APIRouter(prefix="/api/v1/ingredients", tags=["ingredient_detail"])


@router.get("/{ingredient_id}/detail", response_model=IngredientDetailResponse)
async def get_ingredient_detail(ingredient_id: int) -> IngredientDetailResponse:
    """개별 성분 해설·주의사항 — 근거 기반 생성 + 출처 인용. 담당: 호영"""
    return await service.get_ingredient_detail(ingredient_id)
