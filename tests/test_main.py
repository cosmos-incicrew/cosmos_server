from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_health_returns_200_without_auth():
    response = _client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def _reachable() -> bool:
    return True


async def _unreachable() -> bool:
    return False


def test_ready_returns_200_when_supabase_reachable(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module, "_supabase_reachable", _reachable)
    response = _client().get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_when_supabase_down(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module, "_supabase_reachable", _unreachable)
    response = _client().get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NOT_READY"


def test_unknown_route_uses_common_error_format():
    response = _client().get("/no-such-route")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "HTTP_404"
    assert "message" in body["error"]
