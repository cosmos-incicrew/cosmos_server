import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.schemas import ErrorDetail, ErrorResponse
from app.core.config import get_settings
from app.modules.bsti.router import router as bsti_router
from app.modules.ingredient_detail.router import router as ingredient_detail_router
from app.modules.ingredient_search.router import router as ingredient_search_router
from app.modules.product_compare.router import router as product_compare_router
from app.modules.recommendations.router import router as recommendations_router

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cosmos")

app = FastAPI(title="cosmos API", version="0.1.0")

app.include_router(ingredient_search_router)
app.include_router(ingredient_detail_router)
app.include_router(product_compare_router)
app.include_router(bsti_router)
app.include_router(recommendations_router)


def _error_json(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump())


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 우리 코드는 detail을 {"code","message"} dict로 던진다. 그 외(404 등)는 일반 코드로 감싼다.
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return _error_json(exc.status_code, exc.detail["code"], exc.detail.get("message", ""))
    return _error_json(exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _error_json(422, "VALIDATION_ERROR", "요청 형식이 올바르지 않습니다.")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("처리되지 않은 서버 오류")
    return _error_json(500, "INTERNAL_ERROR", "서버 내부 오류가 발생했습니다.")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness — 프로세스가 살아 있는지만 본다. 인증 없이 200."""
    return {"status": "ok"}


# Supabase가 응답하지 않으면 요청을 받아도 처리할 수 없으므로 배포 롤아웃 게이트로 쓴다.
_READINESS_TIMEOUT_SECONDS = 3.0


async def _supabase_reachable() -> bool:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=_READINESS_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{settings.supabase_url}/auth/v1/health")
        return resp.is_success
    except httpx.HTTPError:
        return False


@app.get("/health/ready", tags=["health"])
async def ready() -> JSONResponse:
    """Readiness — 의존 서비스(Supabase)까지 도달 가능한지 확인. 실패 시 503."""
    if not await _supabase_reachable():
        logger.warning("readiness 실패 — Supabase에 연결할 수 없습니다.")
        return _error_json(503, "NOT_READY", "의존 서비스에 연결할 수 없습니다.")
    return JSONResponse(status_code=200, content={"status": "ready"})
