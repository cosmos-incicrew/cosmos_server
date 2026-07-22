from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.core.config import Settings, get_settings

# auto_error=False: 토큰 부재를 403 대신 우리 포맷의 401로 처리하기 위함
_bearer = HTTPBearer(auto_error=False)

# Supabase가 발급하는 액세스 토큰의 고정 audience
_SUPABASE_AUDIENCE = "authenticated"

# Supabase는 액세스 토큰을 비대칭키로 서명한다 — 공개키를 JWKS로 받아 검증한다.
# 레거시 HS256 공유 시크릿(SUPABASE_JWT_SECRET)으로는 검증되지 않는다.
_SIGNING_ALGORITHMS = ["ES256"]


@lru_cache
def _jwk_client(supabase_url: str) -> PyJWKClient:
    """JWKS 클라이언트. 공개키를 캐시하므로 요청마다 JWKS를 다시 받지 않는다."""
    return PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json")


def verify_jwt(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Supabase JWT를 검증하고 user_id(sub)를 반환한다. 보호 라우터 공통 의존성."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_MISSING_TOKEN", "message": "인증 토큰이 없습니다."},
        )
    try:
        signing_key = _jwk_client(settings.supabase_url).get_signing_key_from_jwt(
            credentials.credentials
        )
        payload = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=_SIGNING_ALGORITHMS,
            audience=_SUPABASE_AUDIENCE,
            # PyJWT는 exp가 '있을 때만' 만료를 본다 — 없는 토큰은 영구 유효해진다.
            options={"require": ["exp", "sub"]},
        )
    # PyJWKClientError는 InvalidTokenError의 형제라 따로 잡아야 한다.
    # JWKS 조회 실패도 401이 된다 — 키는 캐시되므로 드물다.
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID_TOKEN", "message": "유효하지 않은 토큰입니다."},
        ) from exc
    # 서명은 유효해도 sub가 없으면 사용자를 특정할 수 없다 — KeyError(500)가 아니라 401로 처리.
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID_TOKEN", "message": "유효하지 않은 토큰입니다."},
        )
    return user_id
