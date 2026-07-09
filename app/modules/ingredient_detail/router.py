from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1/ingredients", tags=["ingredient_detail"])

_NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail={"code": "NOT_IMPLEMENTED", "message": "아직 구현되지 않은 엔드포인트입니다."},
)


@router.get("/{ingredient_id}/detail")
async def get_ingredient_detail(ingredient_id: int) -> None:
    """개별 성분 해설·주의사항 — 근거 기반 생성 + 출처 인용. 담당: 호영"""
    raise _NOT_IMPLEMENTED
