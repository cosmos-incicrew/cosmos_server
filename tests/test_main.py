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


def test_app_shutdown_closes_langfuse(monkeypatch):
    import app.main as main_module

    class FakeSettings:
        langfuse_tracing_enabled = True

    class FakeLangfuse:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    fake = FakeLangfuse()
    monkeypatch.setattr(main_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(main_module, "get_langfuse", lambda: fake)

    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200

    assert fake.shutdown_calls == 1
