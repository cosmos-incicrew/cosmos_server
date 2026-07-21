from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """모든 에러 응답의 공통 포맷: {"error": {"code": ..., "message": ...}}"""

    error: ErrorDetail


class RestrictionRule(BaseModel):
    """성분에 연결된 구조화된 사용제한 규칙."""

    restriction_id: int
    regulate_type: str | None
    provis_atrcl: str | None
    limit_cond: str | None
    is_registered_korea: bool | None
