from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1/products", tags=["product_compare"])

_NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail={"code": "NOT_IMPLEMENTED", "message": "아직 구현되지 않은 엔드포인트입니다."},
)


@router.post("/compare")
async def compare_products() -> None:
    """멀티 제품 교차 조회 — 공통 성분·사용제한은 규칙 기반. 담당: 영기"""
    raise _NOT_IMPLEMENTED
