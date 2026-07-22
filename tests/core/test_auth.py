"""verify_jwt 검증 테스트.

Supabase는 액세스 토큰을 ES256(비대칭)으로 서명하고 공개키를 JWKS로 배포한다.
테스트는 JWKS를 실제로 받지 않고, 로컬에서 만든 EC 키쌍으로 그 구조만 흉내낸다.
"""

import datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core import auth as auth_module
from app.core.auth import verify_jwt
from app.core.config import get_settings

# Supabase 서명키를 대신하는 테스트용 키. _OTHER_KEY는 "다른 키로 서명된 토큰" 재현용.
_SIGNING_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


class _StubJWKClient:
    """PyJWKClient 대역 — 토큰을 안 보고 항상 같은 공개키를 돌려준다."""

    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str):  # noqa: ARG002 - 서명은 jwt.decode가 검증
        return type("_Key", (), {"key": self._public_key})()


@pytest.fixture(autouse=True)
def _stub_jwks(monkeypatch):
    """JWKS 조회를 로컬 공개키로 대체한다 (네트워크 없이 검증)."""
    monkeypatch.setattr(
        auth_module,
        "_jwk_client",
        lambda _url: _StubJWKClient(_SIGNING_KEY.public_key()),
    )


def _make_token(key=_SIGNING_KEY, **overrides) -> str:
    payload = {
        "sub": "user-123",
        "aud": "authenticated",
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        **overrides,
    }
    return jwt.encode(payload, key, algorithm="ES256")


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_valid_token_returns_user_id():
    assert verify_jwt(_creds(_make_token()), get_settings()) == "user-123"


def test_missing_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(None, get_settings())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_MISSING_TOKEN"


def test_token_signed_by_other_key_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(_make_token(key=_OTHER_KEY)), get_settings())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"


def test_expired_token_raises_401():
    token = _make_token(exp=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1))
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(token), get_settings())
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"


def test_wrong_audience_raises_401():
    """다른 용도의 토큰(anon 등)이 흘러들어오면 거부해야 한다."""
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(_make_token(aud="anon")), get_settings())
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"


def test_hs256_token_raises_401():
    """레거시 HS256 토큰은 더 이상 통과하면 안 된다 (알고리즘 혼동 방어)."""
    token = jwt.encode(
        {
            "sub": "user-123",
            "aud": "authenticated",
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        },
        "any-shared-secret",
        algorithm="HS256",
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
        _SIGNING_KEY,
        algorithm="ES256",
    )
    with pytest.raises(HTTPException) as exc_info:
        verify_jwt(_creds(token), get_settings())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "AUTH_INVALID_TOKEN"
