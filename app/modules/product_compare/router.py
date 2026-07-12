from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import verify_jwt
from app.core.config import Settings, get_settings
from app.modules.product_compare import service
from app.modules.product_compare.repository import (
    ProductCompareRepository,
    get_product_compare_repository,
)
from app.modules.product_compare.schemas import ProductCompareRequest, ProductCompareResponse

router = APIRouter(prefix="/api/v1/products", tags=["product_compare"])


@router.post("/compare", response_model=ProductCompareResponse)
async def compare_products(
    request: ProductCompareRequest,
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[ProductCompareRepository, Depends(get_product_compare_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProductCompareResponse:
    """확정된 2개 이상 제품의 성분 포함 관계를 비교한다."""
    del user_id
    try:
        return await service.compare_products(
            repository, request.product_ids, settings.product_compare_max_count
        )
    except service.DuplicateProductIdsError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "DUPLICATE_PRODUCT_IDS",
                "message": "중복된 제품은 비교할 수 없습니다.",
            },
        ) from None
    except service.ProductCompareLimitExceededError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PRODUCT_COMPARE_LIMIT_EXCEEDED",
                "message": "비교 가능한 제품 수를 초과했습니다.",
            },
        ) from None
    except service.ProductNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "제품을 찾을 수 없습니다."},
        ) from None
    except service.ProductNotAnalyzableError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PRODUCT_NOT_ANALYZABLE", "message": "분석할 수 있는 성분이 없습니다."},
        ) from None
