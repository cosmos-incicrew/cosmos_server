from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendation"])

_NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail={"code": "NOT_IMPLEMENTED", "message": "아직 구현되지 않은 엔드포인트입니다."},
)


@router.post("")
async def create_recommendation() -> None:
    """성분 추천 Agent 파이프라인 — 컨텍스트 → retrieval → 안전성 필터 → 생성. 담당: 민경"""
    raise _NOT_IMPLEMENTED
