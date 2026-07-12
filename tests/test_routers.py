import pytest
from fastapi.testclient import TestClient

STUB_ENDPOINTS = [
    ("GET", "/api/v1/ingredients/search"),  # ingredient_search (영기)
    ("GET", "/api/v1/ingredients/1/detail"),  # ingredient_detail (호영)
    ("POST", "/api/v1/bsti/submit"),  # bsti (금별)
    ("POST", "/api/v1/recommendations"),  # recommendations (민경)
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
