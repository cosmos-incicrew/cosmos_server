from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1/ingredients", tags=["ingredient_search"])

_NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail={"code": "NOT_IMPLEMENTED", "message": "아직 구현되지 않은 엔드포인트입니다."},
)


@router.get("/search")
async def search_ingredients() -> None:
    """제품명·성분명 검색 — tsvector 우선, pgvector 폴백. 담당: 영기"""
    raise _NOT_IMPLEMENTED
