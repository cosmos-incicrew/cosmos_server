import pytest
from fastapi.testclient import TestClient

# 구현이 끝난 엔드포인트는 이 목록에서 뺀다 (구현 후에도 501을 기대하면 실패한다).
STUB_ENDPOINTS = [
    ("POST", "/api/v1/bsti/submit"),  # bsti (금별)
]


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("method", "path"), STUB_ENDPOINTS)
def test_stub_endpoints_registered_and_return_501(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 501, f"{path} 미등록 (404) 또는 잘못된 응답"
    assert response.json()["error"]["code"] == "NOT_IMPLEMENTED"
