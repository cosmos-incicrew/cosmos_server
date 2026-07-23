"""retrieval 테스트 — RPC 매핑·score·leg 라우팅·예외 감싸기.

벡터 검색 계약을 고정한다: score 는 RPC 가 준 코사인 유사도 그대로, cases leg 는
원질의, efficacy leg 는 고민 구절로 검색한다.
"""

import pytest

from app.modules.recommendations.constants import EFFICACY_FIELDS, MIN_RETRIEVAL_SCORE
from app.modules.recommendations.pipeline import s3_retrieval
from app.modules.recommendations.pipeline.s3_retrieval import (
    RetrievalCollection,
    retrieve,
    retrieve_for_concerns,
)


class _FakeRpc:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute(self):
        return type("_Resp", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.calls: list[tuple] = []

    def rpc(self, fn: str, params: dict):
        self.calls.append((fn, params))
        return _FakeRpc(self._rows)


@pytest.fixture
def patch_embed(monkeypatch):
    async def _fake_embed(text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(s3_retrieval, "embed_query", _fake_embed)


@pytest.mark.asyncio
async def test_retrieve_cases_maps_rpc_rows_with_real_score(patch_embed, monkeypatch):
    rows = [
        {
            "case_id": "c1", "target_concern": "pores", "skin_type": "지성",
            "skin_concerns": ["모공"], "question": "모공이 넓어요", "answer": "…",
            "cot": [], "recommended_ingredients": ["나이아신아마이드"],
            "evidence_sources": ["PMID:1"], "gender": "female", "age": 30,
            "score": 0.82,
        }
    ]
    client = _FakeClient(rows)

    async def _fake_sb():
        return client

    monkeypatch.setattr(s3_retrieval, "get_supabase", _fake_sb)

    chunks = await retrieve(RetrievalCollection.REC_CASES, "30대 여성 모공", top_k=5)

    assert client.calls[0][0] == "match_rec_cases"
    assert client.calls[0][1]["match_count"] == 5
    assert client.calls[0][1]["query_embedding"] == "[0.1000000,0.2000000,0.3000000]"
    assert len(chunks) == 1
    assert chunks[0].score == 0.82  # 가짜 rank score 가 아니라 RPC 코사인 값


@pytest.mark.asyncio
async def test_retrieve_efficacy_maps_rpc_rows(patch_embed, monkeypatch):
    rows = [
        {
            "id": 42, "inci": "NIACINAMIDE", "name_kor": "나이아신아마이드",
            "efficacy": "피지 조절", "product_traits": "수용성 비타민 B3 유도체입니다.",
            "safety_note": "고농도 자극 가능",
            "recommended_concentration": "2~5%", "recommended_skin_types": "지성",
            "regulation_note": "배합 한도 있음", "reference_source": "PMID:29061803",
            "ingredient_id": 1234, "score": 0.71,
        }
    ]
    client = _FakeClient(rows)

    async def _fake_sb():
        return client

    monkeypatch.setattr(s3_retrieval, "get_supabase", _fake_sb)

    chunks = await retrieve(RetrievalCollection.REC_EFFICACY, "모공 피지", top_k=5)

    assert client.calls[0][0] == "match_rec_efficacy"
    chunk = chunks[0]
    assert chunk.score == 0.71
    assert chunk.metadata["ingredient_id"] == 1234
    assert chunk.metadata["safety_note"] == "고농도 자극 가능"
    # EFFICACY_FIELDS 전량이 metadata 로 넘어가야 ④가 Candidate 를 다 채운다. RPC 가
    # product_traits 를 빠뜨렸을 때 `.get()` 이라 예외 없이 None 이 흘러, ⑩ 선행 서술만
    # 조용히 사라졌다 (migration 017).
    assert set(EFFICACY_FIELDS) <= chunk.metadata.keys()
    assert chunk.metadata["product_traits"] == "수용성 비타민 B3 유도체입니다."


@pytest.mark.asyncio
async def test_empty_result_is_empty_list_not_error(patch_embed, monkeypatch):
    """결과 없음은 예외가 아니라 빈 목록이다 (호출자가 근거 없음을 판단한다)."""
    client = _FakeClient([])

    async def _fake_sb():
        return client

    monkeypatch.setattr(s3_retrieval, "get_supabase", _fake_sb)

    assert await retrieve(RetrievalCollection.REC_CASES, "모공", top_k=3) == []


@pytest.mark.asyncio
async def test_infrastructure_failure_wrapped_in_retrieval_error(patch_embed, monkeypatch):
    async def _fake_sb():
        raise RuntimeError("db down")

    monkeypatch.setattr(s3_retrieval, "get_supabase", _fake_sb)

    with pytest.raises(s3_retrieval.RetrievalError):
        await retrieve(RetrievalCollection.REC_EFFICACY, "모공", top_k=5)


@pytest.mark.asyncio
async def test_cot_non_string_steps_do_not_leak_repr(patch_embed, monkeypatch):
    """cot 원소가 dict 면 근거 텍스트에 {'step': ...} repr 이 그대로 들어가 프롬프트로 간다."""
    rows = [
        {
            "case_id": "c1", "target_concern": "모공", "skin_type": "지성",
            "skin_concerns": ["모공"], "question": "질문", "answer": "답변",
            "cot": [{"step": 1}, {"step": 2}], "recommended_ingredients": [],
            "evidence_sources": [], "gender": "여성", "age": 30, "score": 0.6,
        }
    ]
    client = _FakeClient(rows)

    async def _fake_sb():
        return client

    monkeypatch.setattr(s3_retrieval, "get_supabase", _fake_sb)

    chunks = await retrieve(RetrievalCollection.REC_CASES, "모공", top_k=3)

    assert "{'step'" not in chunks[0].content


@pytest.mark.asyncio
async def test_efficacy_chunk_falls_back_to_inci_when_no_korean_name(patch_embed, monkeypatch):
    rows = [
        {
            "id": 1, "inci": "NIACINAMIDE", "name_kor": None, "efficacy": "피지 조절",
            "safety_note": None, "recommended_concentration": None,
            "recommended_skin_types": None, "regulation_note": None,
            "reference_source": None, "ingredient_id": 1, "score": 0.5,
        }
    ]
    client = _FakeClient(rows)

    async def _fake_sb():
        return client

    monkeypatch.setattr(s3_retrieval, "get_supabase", _fake_sb)

    chunks = await retrieve(RetrievalCollection.REC_EFFICACY, "모공", top_k=5)

    assert "NIACINAMIDE" in chunks[0].content


@pytest.mark.asyncio
async def test_retrieve_one_routes_leg_specific_queries(monkeypatch):
    """cases leg 는 원질의(사람묘사+고민), efficacy leg 는 고민 구절로 검색한다."""
    calls: list[tuple] = []

    async def _spy_retrieve(collection, query_text, top_k):
        calls.append((collection, query_text))
        return []

    monkeypatch.setattr(s3_retrieval, "retrieve", _spy_retrieve)

    # pores → CONCERN_SEARCH_KEYWORDS = ("모공", "피지") → "모공 피지"
    await s3_retrieval._retrieve_one("pores", "30대 여성 모공 관리에 도움되는 성분")

    by_collection = {c: q for c, q in calls}
    assert by_collection[RetrievalCollection.REC_CASES] == "30대 여성 모공 관리에 도움되는 성분"
    assert by_collection[RetrievalCollection.REC_EFFICACY] == "모공 피지"


@pytest.mark.asyncio
async def test_retrieve_one_falls_back_to_concern_label_when_no_keywords(monkeypatch):
    """CONCERN_SEARCH_KEYWORDS 에 없는 고민 코드는 라벨로 efficacy leg 를 검색한다."""
    calls: list[tuple] = []

    async def _spy_retrieve(collection, query_text, top_k):
        calls.append((collection, query_text))
        return []

    monkeypatch.setattr(s3_retrieval, "retrieve", _spy_retrieve)
    monkeypatch.setitem(s3_retrieval.CONCERN_SEARCH_KEYWORDS, "pores", ())

    await s3_retrieval._retrieve_one("pores", "질의")

    by_collection = {c: q for c, q in calls}
    assert by_collection[RetrievalCollection.REC_EFFICACY] == s3_retrieval.CONCERN_LABEL_BY_CODE[
        "pores"
    ]


# ── 저score 컷 (MIN_RETRIEVAL_SCORE) ────────────────────────────


def _scored_chunk(doc_id: str, score: float):
    from app.modules.recommendations.schemas import ChunkSource, RetrievedChunk

    return RetrievedChunk(
        content="근거", score=score, source=ChunkSource(doc_id=doc_id, title="t"), metadata={}
    )


@pytest.mark.asyncio
async def test_chunks_below_min_score_are_cut(monkeypatch):
    """임계값 미만 근거는 버린다 — 남기면 관련 없는 자료로 서사를 생성한다."""
    low = MIN_RETRIEVAL_SCORE - 0.01
    high = MIN_RETRIEVAL_SCORE + 0.01

    async def _one(code, query):
        return code, [_scored_chunk("case_low", low), _scored_chunk("case_high", high)], []

    monkeypatch.setattr(s3_retrieval, "_retrieve_one", _one)

    cases, _ = await retrieve_for_concerns([("pores", "q")])

    assert [c.source.doc_id for c in cases] == ["case_high"]


@pytest.mark.asyncio
async def test_threshold_pass_tags_chunk_with_its_concern(monkeypatch):
    """같은 함수가 concern 태깅도 맡는다 — 죽으면 ⑩ advisory 가 모든 고민을 누락으로 본다."""

    async def _one(code, query):
        return code, [], [_scored_chunk(f"eff_{code}", 0.9)]

    monkeypatch.setattr(s3_retrieval, "_retrieve_one", _one)

    _, efficacy = await retrieve_for_concerns([("pores", "q1"), ("acne", "q2")])

    assert [c.metadata["concern"] for c in efficacy] == ["pores", "acne"]
