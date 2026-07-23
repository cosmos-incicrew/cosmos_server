import pytest

from app.modules.recommendations.embedding import embed_query, to_pgvector


class _FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeResponse:
    def __init__(self, values: list[float]) -> None:
        self.embeddings = [_FakeEmbedding(values)]


@pytest.mark.asyncio
async def test_embed_query_uses_query_task_dim_and_normalizes(monkeypatch):
    captured: dict = {}

    class _FakeModels:
        async def embed_content(self, *, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["task_type"] = config.task_type
            captured["dim"] = config.output_dimensionality
            return _FakeResponse([3.0, 4.0])  # norm 5 → [0.6, 0.8]

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = type("_Aio", (), {"models": _FakeModels()})()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )

    vec = await embed_query("모공 피지")

    assert captured["task_type"] == "RETRIEVAL_QUERY"
    assert captured["dim"] == 1536
    assert captured["model"] == "gemini-embedding-001"
    assert vec[0] == pytest.approx(0.6)
    assert vec[1] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_embed_query_replaces_empty_text(monkeypatch):
    captured: dict = {}

    class _FakeModels:
        async def embed_content(self, *, model, contents, config):
            captured["contents"] = contents
            return _FakeResponse([1.0, 0.0])

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = type("_Aio", (), {"models": _FakeModels()})()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )

    await embed_query("")
    assert captured["contents"] == " "  # 빈 문자열은 embed API 가 거부하므로 공백으로


def test_to_pgvector_literal():
    assert to_pgvector([0.5, -0.25]) == "[0.5000000,-0.2500000]"


def test_embed_documents_uses_document_task(monkeypatch):
    tasks: list[str] = []

    class _FakeModels:
        def embed_content(self, *, model, contents, config):
            tasks.append(config.task_type)
            return _FakeResponse([3.0, 4.0])

    class _FakeClient:
        def __init__(self) -> None:
            self.models = _FakeModels()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )

    from app.modules.recommendations.embedding import embed_documents

    vecs = embed_documents(["레티놀 주름", "나이아신아마이드 미백"])
    assert tasks == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_DOCUMENT"]
    assert vecs[0][0] == pytest.approx(0.6)  # 정규화 동일 규칙


def test_embed_documents_retries_transient_error(monkeypatch):
    """깊은 지점의 transient 실패가 배치 전체를 죽이지 않는다 (백오프 흡수)."""
    calls = {"n": 0}

    class _FakeModels:
        def embed_content(self, *, model, contents, config):
            calls["n"] += 1
            if calls["n"] < 3:  # 처음 두 번은 429 흉내
                raise RuntimeError("429 Too Many Requests")
            return _FakeResponse([3.0, 4.0])

    class _FakeClient:
        def __init__(self) -> None:
            self.models = _FakeModels()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )
    monkeypatch.setattr("app.modules.recommendations.embedding.time.sleep", lambda _s: None)

    from app.modules.recommendations.embedding import embed_documents

    vecs = embed_documents(["레티놀"])
    assert calls["n"] == 3  # 2회 실패 후 3번째 성공
    assert vecs[0][0] == pytest.approx(0.6)


def test_embed_documents_raises_after_five_failures(monkeypatch):
    calls = {"n": 0}

    class _FakeModels:
        def embed_content(self, *, model, contents, config):
            calls["n"] += 1
            raise RuntimeError("persistent")

    class _FakeClient:
        def __init__(self) -> None:
            self.models = _FakeModels()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )
    monkeypatch.setattr("app.modules.recommendations.embedding.time.sleep", lambda _s: None)

    from app.modules.recommendations.embedding import embed_documents

    with pytest.raises(RuntimeError, match="persistent"):
        embed_documents(["레티놀"])
    assert calls["n"] == 5  # 5회 시도 후 포기


@pytest.mark.asyncio
async def test_embed_query_retries_once(monkeypatch):
    """질의 경로는 1회 재시도(총 2회)로 transient 오류를 흡수한다."""
    calls = {"n": 0}

    class _FakeModels:
        async def embed_content(self, *, model, contents, config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429")
            return _FakeResponse([3.0, 4.0])

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = type("_Aio", (), {"models": _FakeModels()})()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("app.modules.recommendations.embedding.asyncio.sleep", _no_sleep)

    vec = await embed_query("모공")
    assert calls["n"] == 2
    assert vec[0] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_embed_query_raises_after_two_failures(monkeypatch):
    calls = {"n": 0}

    class _FakeModels:
        async def embed_content(self, *, model, contents, config):
            calls["n"] += 1
            raise RuntimeError("persistent")

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = type("_Aio", (), {"models": _FakeModels()})()

    monkeypatch.setattr(
        "app.modules.recommendations.embedding.get_gemini", lambda: _FakeClient()
    )

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("app.modules.recommendations.embedding.asyncio.sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="persistent"):
        await embed_query("모공")
    assert calls["n"] == 2  # 질의 경로는 상한 2회
