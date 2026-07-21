from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import verify_jwt
from app.modules.ingredient_detail import service
from app.modules.ingredient_detail.schemas import (
    IngredientDetailResponse,
    ProductSummaryRequest,
    ProductSummaryResponse,
)

router = APIRouter(prefix="/api/v1/ingredients", tags=["ingredient_detail"])

_INGREDIENT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail={
        "code": "INGREDIENT_NOT_FOUND",
        "message": "성분을 찾을 수 없습니다.",
    },
)
_EVIDENCE_UNAVAILABLE = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail={
        "code": "EVIDENCE_UNAVAILABLE",
        "message": "성분 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
    },
)
_GENERATION_FAILED = HTTPException(
    status_code=status.HTTP_502_BAD_GATEWAY,
    detail={
        "code": "GENERATION_FAILED",
        "message": "해설을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    },
)


@router.get("/{ingredient_id}/detail", response_model=IngredientDetailResponse)
async def get_ingredient_detail(
    ingredient_id: int,
    user_id: Annotated[str, Depends(verify_jwt)],
) -> IngredientDetailResponse:
    """개별 성분 해설·주의사항 — 근거 기반 생성 + 출처 인용. 담당: 호영

    성분 해설은 공용(카탈로그) 데이터라 user_id로 필터링하지 않는다.
    로그인 인증만 요구한다.
    """
    del user_id
    try:
        return await service.get_ingredient_detail(ingredient_id)
    except service.IngredientNotFoundError:
        raise _INGREDIENT_NOT_FOUND from None
    except service.EvidenceUnavailableError:
        raise _EVIDENCE_UNAVAILABLE from None
    except service.GenerationFailedError:
        raise _GENERATION_FAILED from None


@router.post("/product-summary", response_model=ProductSummaryResponse)
async def get_product_summary(
    body: ProductSummaryRequest,
    user_id: Annotated[str, Depends(verify_jwt)],
) -> ProductSummaryResponse:
    """제품 요약 — 전성분(배합순)을 종합해 대표성분 + 제품 해설 요약. 담당: 호영

    공용 데이터라 user_id로 필터링하지 않고, 로그인 인증만 요구한다.
    """
    del user_id
    try:
        return await service.get_product_summary(body.ingredient_ids)
    except service.IngredientNotFoundError:
        raise _INGREDIENT_NOT_FOUND from None
    except service.EvidenceUnavailableError:
        raise _EVIDENCE_UNAVAILABLE from None
    except service.GenerationFailedError:
        raise _GENERATION_FAILED from None
