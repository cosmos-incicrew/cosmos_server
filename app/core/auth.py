from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

# auto_error=False: 토큰 부재를 403 대신 우리 포맷의 401로 처리하기 위함
_bearer = HTTPBearer(auto_error=False)

# Supabase가 발급하는 액세스 토큰의 고정 audience
_SUPABASE_AUDIENCE = "authenticated"


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
        payload = jwt.decode(
            credentials.credentials,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience=_SUPABASE_AUDIENCE,
        )
    except jwt.InvalidTokenError as exc:
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
