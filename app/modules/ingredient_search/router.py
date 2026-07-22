from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import verify_jwt
from app.modules.ingredient_search import service
from app.modules.ingredient_search.repository import (
    IngredientSearchRepository,
    get_ingredient_search_repository,
)
from app.modules.ingredient_search.schemas import (
    IngredientSearchResponse,
    ProductIngredientIdsResponse,
    ProductSearchResponse,
)

router = APIRouter(tags=["ingredient_search"])
product_router = APIRouter(prefix="/api/v1/products")
ingredient_router = APIRouter(prefix="/api/v1/ingredients")
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50


@product_router.get("/search", response_model=ProductSearchResponse)
async def search_products(
    q: Annotated[str, Query(min_length=1)],
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[IngredientSearchRepository, Depends(get_ingredient_search_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_SEARCH_LIMIT,
) -> ProductSearchResponse:
    """분석 가능한 제품 후보를 제품명으로 검색한다."""
    del user_id
    try:
        return await service.search_products(repository, q, limit)
    except service.ProductQueryTooLongError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "QUERY_TOO_LONG", "message": "검색어는 100자 이하여야 합니다."},
        ) from None
    except service.ProductQueryTooShortError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "QUERY_TOO_SHORT", "message": "검색어는 2자 이상이어야 합니다."},
        ) from None


@product_router.get("/{product_id}/ingredients", response_model=ProductIngredientIdsResponse)
async def get_product_ingredient_ids(
    product_id: int,
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[IngredientSearchRepository, Depends(get_ingredient_search_repository)],
) -> ProductIngredientIdsResponse:
    """선택된 제품을 후속 조회용 확정 성분 ID 목록으로 변환한다."""
    del user_id
    try:
        return await service.get_product_ingredient_ids(repository, product_id)
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


@ingredient_router.get("/search", response_model=IngredientSearchResponse)
async def search_ingredients(
    q: Annotated[str, Query(min_length=1)],
    user_id: Annotated[str, Depends(verify_jwt)],
    repository: Annotated[IngredientSearchRepository, Depends(get_ingredient_search_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_SEARCH_LIMIT,
) -> IngredientSearchResponse:
    """성분 이명으로 후보 성분을 검색한다."""
    del user_id
    return await service.search_ingredients(repository, q, limit)


router.include_router(product_router)
router.include_router(ingredient_router)
