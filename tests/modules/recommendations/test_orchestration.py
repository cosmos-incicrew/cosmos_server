"""service.py 배선 테스트 — 7단계가 올바른 순서로 이어지는지.

단계별 테스트는 각 단계를 잘 검증하지만 **연결**은 검증하지 못한다. 실제로 이 파일이
생기기 전에는 오케스트레이터에서 ⑤ 안전성 필터 호출을 통째로 지워도 121개 테스트가
전부 통과했다. 여기서는 모든 단계를 목으로 두고 호출 순서와 조기 종료만 본다.
"""

import pytest

from app.modules.recommendations import service
from app.modules.recommendations.pipeline import (
    s1_context,
    s2_queries,
    s3_retrieval,
    s4_candidates,
    s5_safety,
    s6_generation,
    s7_response,
    s8_products,
)
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    LlmNarrative,
    RecommendationResponse,
    RetrievedChunk,
    UserContext,
    UserProfile,
)

_USER = "22222222-2222-4222-8222-222222222222"


def _chunk(doc_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        content="근거", score=0.9, source=ChunkSource(doc_id=doc_id, title="t"), metadata={}
    )


def _ok_response(status: str = "ok") -> RecommendationResponse:
    return RecommendationResponse(status=status, user_profile=UserProfile(), disclaimer="d")


@pytest.fixture()
def stages(monkeypatch: pytest.MonkeyPatch) -> dict:
    """전 단계를 목으로 대체하고 호출 순서를 기록한다. 기본은 정상 경로."""
    calls: list[str] = []
    state: dict = {
        "calls": calls,
        "cases": [_chunk("case_1")],
        "efficacy": [_chunk("eff_1")],
        "candidates": [Candidate(name_kor="판테놀", score=0.9)],
        "safe": [Candidate(name_kor="판테놀", score=0.9)],
        "narrative": LlmNarrative(
            cause_analysis="원인", recommendation="판테놀 추천", usage_guide="사용법",
            recommended_names=["판테놀"],
        ),
    }

    async def _build_context(user_id: str) -> UserContext:
        calls.append("s1_context")
        assert user_id == _USER, "user_id 가 ① 로 그대로 전달돼야 한다"
        return UserContext(user_id=user_id, age=32, concerns=["pores"])

    def _build_queries(context: UserContext) -> list[tuple[str, str]]:
        calls.append("s2_queries")
        return [("pores", "모공 질의")]

    async def _retrieve(queries):
        calls.append("s3_retrieval")
        return state["cases"], state["efficacy"]

    async def _aggregate(case_chunks, efficacy_chunks, context):
        calls.append("s4_candidates")
        return state["candidates"]

    async def _safety(candidates, context):
        calls.append("s5_safety")
        return state["safe"]

    async def _generate(context, candidates, chunks):
        calls.append("s6_generation")
        state["generated_with"] = candidates
        return state["narrative"]

    async def _fetch_products(candidates, narrative, owned_product_ids):
        calls.append("s8_products")
        state["products_from"] = candidates
        return []

    def _assemble(context, candidates, narrative, case_chunks, efficacy_chunks, products):
        calls.append("s7_response.assemble")
        state["assembled_with"] = candidates
        return _ok_response()

    def _insufficient(context, code, message, action=None):
        calls.append("s7_response.insufficient")
        state["insufficient_code"] = code
        state["insufficient_message"] = message
        state["insufficient_action"] = action
        return _ok_response("insufficient_evidence")

    monkeypatch.setattr(s1_context, "build_context", _build_context)
    monkeypatch.setattr(s2_queries, "build_queries", _build_queries)
    monkeypatch.setattr(s3_retrieval, "retrieve_for_concerns", _retrieve)
    monkeypatch.setattr(s4_candidates, "aggregate_candidates", _aggregate)
    monkeypatch.setattr(s5_safety, "apply_safety_filters", _safety)
    monkeypatch.setattr(s6_generation, "generate", _generate)
    monkeypatch.setattr(s8_products, "fetch", _fetch_products)
    monkeypatch.setattr(s7_response, "assemble", _assemble)
    monkeypatch.setattr(s7_response, "insufficient", _insufficient)
    return state


async def test_happy_path_runs_all_seven_stages_in_order(stages):
    response = await service.create_recommendations(_USER)

    assert stages["calls"] == [
        "s1_context",
        "s2_queries",
        "s3_retrieval",
        "s4_candidates",
        "s5_safety",
        "s6_generation",
        "s8_products",
        "s7_response.assemble",
    ]
    assert response.status == "ok"


async def test_safety_filter_output_feeds_generation_and_assembly(stages):
    """④ 원본이 아니라 ⑤ 통과분이 ⑥·⑦ 로 가야 한다.

    안전성 필터를 건너뛰거나 필터 이전 목록을 넘기면 금지 성분이 그대로 추천된다.
    """
    filtered = [Candidate(name_kor="판테놀", score=0.9)]
    stages["candidates"] = [
        Candidate(name_kor="판테놀", score=0.9),
        Candidate(name_kor="금지성분", score=0.8),
    ]
    stages["safe"] = filtered

    await service.create_recommendations(_USER)

    assert stages["generated_with"] is filtered, "⑥ 은 ⑤ 통과분을 받아야 한다"
    assert stages["assembled_with"] is filtered, "⑦ 도 ⑤ 통과분을 받아야 한다"


async def test_no_evidence_skips_generation(stages):
    """근거가 없으면 LLM 을 부르지 않는다 (llm-rag-rules · 비용 보호)."""
    stages["cases"] = []
    stages["efficacy"] = []

    response = await service.create_recommendations(_USER)

    assert "s6_generation" not in stages["calls"]
    assert "s4_candidates" not in stages["calls"]
    assert stages["calls"][-1] == "s7_response.insufficient"
    assert response.status == "insufficient_evidence"


async def test_partial_evidence_still_proceeds(stages):
    """한쪽 인덱스만 비어도 진행한다 — 둘 다 비었을 때만 확인 불가."""
    stages["cases"] = []

    await service.create_recommendations(_USER)

    assert "s6_generation" in stages["calls"]


async def test_all_candidates_filtered_out_skips_generation(stages):
    """⑤ 후 후보가 0개면 생성 없이 확인 불가 + 다른 고민 유도."""
    stages["safe"] = []

    response = await service.create_recommendations(_USER)

    assert "s6_generation" not in stages["calls"]
    assert stages["insufficient_action"] == "retry_with_other_concerns"
    assert response.status == "insufficient_evidence"


async def test_generation_receives_both_chunk_sets(stages, monkeypatch: pytest.MonkeyPatch):
    """⑥ 프롬프트 근거는 사례 + 효능 두 인덱스를 합친 것이어야 한다."""
    captured: dict = {}

    async def _generate(context, candidates, chunks):
        captured["chunks"] = chunks
        return stages["narrative"]

    monkeypatch.setattr(s6_generation, "generate", _generate)

    await service.create_recommendations(_USER)

    doc_ids = {c.source.doc_id for c in captured["chunks"]}
    assert doc_ids == {"case_1", "eff_1"}
