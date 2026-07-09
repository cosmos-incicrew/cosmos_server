from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1/bsti", tags=["bsti"])

_NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    detail={"code": "NOT_IMPLEMENTED", "message": "아직 구현되지 않은 엔드포인트입니다."},
)


@router.post("/submit")
async def submit_bsti_survey() -> None:
    """BSTI 설문 제출 → 16타입 산출. 담당: 금별 (설문 문항 확정 대기)"""
    raise _NOT_IMPLEMENTED
