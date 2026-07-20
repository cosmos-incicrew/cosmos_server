from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import verify_jwt
from app.modules.ingredient_detail import service
from app.modules.ingredient_detail.schemas import (
    IngredientDetailResponse,
    ProductSummaryRequest,
    ProductSummaryResponse,
)

router = APIRouter(prefix="/api/v1/ingredients", tags=["ingredient_detail"])


@router.get("/{ingredient_id}/detail", response_model=IngredientDetailResponse)
async def get_ingredient_detail(
    ingredient_id: int, user_id: Annotated[str, Depends(verify_jwt)]
) -> IngredientDetailResponse:
    """개별 성분 해설·주의사항 — 근거 기반 생성 + 출처 인용."""
    del user_id
    return await service.get_ingredient_detail(ingredient_id)


@router.post("/product-summary", response_model=ProductSummaryResponse)
async def get_product_summary(
    body: ProductSummaryRequest, user_id: Annotated[str, Depends(verify_jwt)]
) -> ProductSummaryResponse:
    """제품 요약 — 전성분(배합순)을 종합해 대표성분 + 제품 해설 요약."""
    del user_id
    return await service.get_product_summary(body.ingredient_ids)
