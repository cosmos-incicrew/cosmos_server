from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """모든 에러 응답의 공통 포맷: {"error": {"code": ..., "message": ...}}"""

    error: ErrorDetail
