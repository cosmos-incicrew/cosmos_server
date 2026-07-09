import datetime

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import verify_jwt
from app.core.config import get_settings
from tests.conftest import TEST_ENV


def _make_token(secret: str, **overrides) -> str:
    payload = {
        "sub": "user-123",
        "aud": "authenticated",
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        **overrides,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_valid_token_returns_user_id():
    token = _make_token(TEST_ENV["SUPABASE_JWT_SECRET"])
    assert verify_jwt(_creds(token), get_settings()) == "user-123"


def test_missing_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(None, get_settings())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_MISSING_TOKEN"


def test_wrong_secret_raises_401():
    token = _make_token("wrong-secret")
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(token), get_settings())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"


def test_expired_token_raises_401():
    token = _make_token(
        TEST_ENV["SUPABASE_JWT_SECRET"],
        exp=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1),
    )
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(token), get_settings())
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"


def test_token_without_sub_raises_401():
    """서명은 유효하나 sub가 없는 토큰은 500이 아니라 401이어야 한다."""
    token = jwt.encode(
        {
            "aud": "authenticated",
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        },
        TEST_ENV["SUPABASE_JWT_SECRET"],
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(token), get_settings())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"
