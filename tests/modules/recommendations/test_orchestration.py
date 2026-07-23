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
    s7_bsti,
    s8_top_picks,
    s9_products,
    s10_response,
)
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    LlmNarrative,
    ProductRecommendation,
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
        "products_calls": [],
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

    async def _generate(context, candidates, chunks, bsti_candidates=None):
        calls.append("s6_generation")
        state["generated_with"] = candidates
        state["generated_bsti"] = bsti_candidates
        return state["narrative"]

    async def _bsti_candidates(context):
        """⑦ BSTI 가지. ③ 검색과 병렬이라 calls 에 넣지 않는다 — 순서가 비결정적이다.

        후보 0개를 돌려주면 가지가 ⑤·⑨을 부르지 않아 다른 단계의 호출 기록도 안 흐린다.
        """
        state["bsti_context"] = context
        return []

    async def _fetch_products(candidates, chosen_names, owned_product_ids):
        """⑨ 은 ⑧ 종합 제품 한 번만 불린다 (축별 제품 목록이 응답에서 빠졌다).

        어느 이름 집합으로 조회했는지 드러나야 오배선을 잡을 수 있어, 요청한 이름을
        결과 제품에 그대로 심는다. 이름이 없으면 빈 목록인 것도 실제 구현과 같다
        (조인 키가 없어 조회 자체를 건너뛴다).
        """
        calls.append("s9_products")
        state["products_calls"].append({"candidates": candidates, "names": chosen_names})
        if not chosen_names:
            return []
        return [
            ProductRecommendation(
                product_id=100 + len(state["products_calls"]),
                product_name="제품",
                matched_ingredients=sorted(chosen_names),
            )
        ]

    def _assemble(
        context,
        narrative,
        case_chunks,
        efficacy_chunks,
        top_picks=None,
        top_products=None,
    ):
        calls.append("s10_response.assemble")
        state["assembled_top"] = (top_picks, top_products)
        return _ok_response()

    def _insufficient(context, code, message, action=None):
        calls.append("s10_response.insufficient")
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
    monkeypatch.setattr(s7_bsti, "fetch_bsti_candidates", _bsti_candidates)
    monkeypatch.setattr(s9_products, "fetch", _fetch_products)
    monkeypatch.setattr(s10_response, "assemble", _assemble)
    monkeypatch.setattr(s10_response, "insufficient", _insufficient)
    return state


async def test_happy_path_runs_all_stages_in_order(stages):
    response = await service.create_recommendations(_USER)

    # ⑨은 ⑧ 종합 제품 한 번뿐이다 — 축별 제품 목록이 응답에서 빠지며 3회→1회로 줄었다.
    assert stages["calls"] == [
        "s1_context",
        "s2_queries",
        "s3_retrieval",
        "s4_candidates",
        "s5_safety",
        "s6_generation",
        "s9_products",
        "s10_response.assemble",
    ]
    assert response.status == "ok"


async def test_safety_filter_output_feeds_generation_and_top_picks(stages):
    """④ 원본이 아니라 ⑤ 통과분이 ⑥·⑧ 로 가야 한다.

    안전성 필터를 건너뛰거나 필터 이전 목록을 넘기면 금지 성분이 그대로 추천된다.
    ⑩은 더 이상 후보 목록을 받지 않으므로(성분 배열 삭제, 2026-07-23) 통과분이 응답에
    닿는 경로는 ⑧ 대표 성분뿐이다. ⑥ 서사가 금지 성분까지 언급했다고 두어, 호출부가
    ④ 원본을 ⑧에 넘기면 그 성분이 메인 카드로 새는 것을 잡는다.
    """
    filtered = [Candidate(name_kor="판테놀", score=0.9)]
    stages["candidates"] = [
        Candidate(name_kor="판테놀", score=0.9),
        Candidate(name_kor="금지성분", score=0.95),
    ]
    stages["safe"] = filtered
    stages["narrative"] = LlmNarrative(
        cause_analysis="원인", recommendation="판테놀·금지성분 추천", usage_guide="사용법",
        recommended_names=["판테놀", "금지성분"],
    )

    await service.create_recommendations(_USER)

    assert stages["generated_with"] is filtered, "⑥ 은 ⑤ 통과분을 받아야 한다"
    top_picks, _ = stages["assembled_top"]
    assert [c.name_kor for c, _ in top_picks] == ["판테놀"], "⑧ 대표 성분도 ⑤ 통과분에서만"


async def test_no_evidence_skips_generation(stages):
    """근거가 없으면 LLM 을 부르지 않는다 (llm-rag-rules · 비용 보호)."""
    stages["cases"] = []
    stages["efficacy"] = []

    response = await service.create_recommendations(_USER)

    assert "s6_generation" not in stages["calls"]
    assert "s4_candidates" not in stages["calls"]
    assert stages["calls"][-1] == "s10_response.insufficient"
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


def _patch_bsti_branch(monkeypatch, candidates):
    """⑦ 가지 전체를 정해진 산출로 대체한다 (가지 내부는 test_bsti_recommendation 담당)."""

    async def _branch(user):
        return candidates

    monkeypatch.setattr(service, "_bsti_branch", _branch)


async def test_top_recommendation_reaches_assembly(stages):
    """⑧ 산출이 ⑩까지 도달해야 한다.

    `select_top()` 자체는 test_top_recommendation 이 직접 부르며 검증하지만, 호출부에서
    `top_picks = []` / `top_names = set()` 으로 죽여도 그 테스트는 전부 통과한다 —
    프론트 메인 카드가 통째로 비는 회귀를 여기서 잡는다.
    """
    await service.create_recommendations(_USER)

    top_picks, top_products = stages["assembled_top"]
    assert [(c.name_kor, src) for c, src in top_picks] == [("판테놀", s8_top_picks.SOURCE_CONCERN)]
    assert [p.matched_ingredients for p in top_products] == [["판테놀"]], (
        "⑧ 대표 성분(top_names)으로 조회한 제품이 메인 카드로 가야 한다"
    )


async def test_bsti_branch_output_reaches_assembly(stages, monkeypatch: pytest.MonkeyPatch):
    """⑦ 가지의 성분이 ⑧을 거쳐 ⑩까지 닿아야 한다 (중간에 새면 타입 추천이 통째로 빈다).

    축별 배열이 사라진 뒤로 BSTI 는 오직 이 경로로만 응답에 나타난다.
    """
    _patch_bsti_branch(monkeypatch, [Candidate(name_kor="세라마이드", score=1.0)])

    await service.create_recommendations(_USER)

    top_picks, _ = stages["assembled_top"]
    assert ("세라마이드", s8_top_picks.SOURCE_BSTI) in [(c.name_kor, src) for c, src in top_picks]


async def test_top_product_lookup_gets_candidates_from_both_axes(
    stages, monkeypatch: pytest.MonkeyPatch
):
    """⑧ 제품 조회는 고민 후보 + BSTI 후보를 합쳐 받아야 한다.

    ⑨은 `ingredient_id` 로 조인하므로 BSTI 후보를 빼면 BSTI 대표 성분의 제품이
    메인 카드에서 통째로 사라진다.
    """
    bsti = [Candidate(name_kor="세라마이드", score=1.0)]
    _patch_bsti_branch(monkeypatch, bsti)

    await service.create_recommendations(_USER)

    top_call = next(c for c in stages["products_calls"] if "세라마이드" in c["names"])
    assert [c.name_kor for c in top_call["candidates"]] == ["판테놀", "세라마이드"]


async def test_generation_receives_both_chunk_sets(stages, monkeypatch: pytest.MonkeyPatch):
    """⑥ 프롬프트 근거는 사례 + 효능 두 인덱스를 합친 것이어야 한다."""
    captured: dict = {}

    async def _generate(context, candidates, chunks, bsti_candidates=None):
        captured["chunks"] = chunks
        return stages["narrative"]

    monkeypatch.setattr(s6_generation, "generate", _generate)

    await service.create_recommendations(_USER)

    doc_ids = {c.source.doc_id for c in captured["chunks"]}
    assert doc_ids == {"case_1", "eff_1"}


async def test_bsti_branch_output_reaches_generation(stages, monkeypatch: pytest.MonkeyPatch):
    """⑦ 가지 산출이 ⑥ 시작 시점에 이미 있어야 한다 (BSTI 축 성분의 영어 원문 번역).

    ⑦이 ⑥과 `asyncio.gather` 로 병렬이던 시절엔 프롬프트를 조립할 때 BSTI 후보가
    없었다. 병렬로 되돌리면 여기서 None 이 잡힌다.
    """
    bsti = [Candidate(name_kor="병풀추출물", score=1.0)]
    _patch_bsti_branch(monkeypatch, bsti)

    await service.create_recommendations(_USER)

    assert stages["generated_bsti"] == bsti


async def test_bsti_branch_runs_even_without_search_results(stages, monkeypatch):
    """⑦은 ③ 과 병렬이라 검색 결과와 무관하게 돈다 — 순차로 붙이면 왕복이 더해진다."""
    stages["cases"] = []
    stages["efficacy"] = []

    await service.create_recommendations(_USER)

    assert stages["bsti_context"].user_id == _USER


# ── ⑥ 재생성 판정 — 고민 커버리지 (설계 04 §10) ─────────────────
#
# ④가 고민당 슬롯을 예약해도 ⑥이 최종 3개를 한쪽 고민으로 몰아 고르면 그대로 응답이
# 된다 (실행: 고민이 redness+brightening 인데 미백 2 + 진정 1). 프롬프트의 "고민마다
# 최소 하나"는 요청이지 보장이 아니므로, `too_few` 와 같은 자리에서 재생성으로 막는다.


def _pick(*names: str) -> LlmNarrative:
    return LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=list(names),
    )


async def _generate_recording_attempts(
    monkeypatch: pytest.MonkeyPatch,
    candidates: list[Candidate],
    context: UserContext,
    *outputs: LlmNarrative,
) -> int:
    """⑥을 실제로 돌리고 LLM 호출 횟수를 돌려준다 (출력은 순서대로 소진)."""
    calls = {"n": 0}
    queue = list(outputs)

    async def _fake(prompt, ctx, cands):
        calls["n"] += 1
        return queue.pop(0) if queue else outputs[-1]

    monkeypatch.setattr(s6_generation, "_call_gemini", _fake)
    await s6_generation.generate(context, candidates, [])
    return calls["n"]


def _c(name: str, *concerns: str) -> Candidate:
    return Candidate(name_kor=name, score=0.72, concerns=list(concerns))


async def test_uncovered_concern_triggers_regeneration(monkeypatch: pytest.MonkeyPatch):
    """고민 둘 중 하나만 커버한 결과는 다시 뽑는다."""
    candidates = [
        _c("헥사펩타이드-2", "brightening"),
        _c("레조시놀", "brightening"),
        _c("알파-알부틴", "brightening"),
        _c("수국잎추출물", "redness"),
    ]
    context = UserContext(user_id="u", concerns=["redness", "brightening"])

    calls = await _generate_recording_attempts(
        monkeypatch, candidates, context,
        _pick("헥사펩타이드-2", "레조시놀", "알파-알부틴"),  # 미백만 — 진정 미커버
        _pick("헥사펩타이드-2", "레조시놀", "수국잎추출물"),
    )

    assert calls == 2


async def test_balanced_pick_is_returned_without_regeneration(monkeypatch: pytest.MonkeyPatch):
    candidates = [_c("헥사펩타이드-2", "brightening"), _c("수국잎추출물", "redness")]
    context = UserContext(user_id="u", concerns=["redness", "brightening"])

    calls = await _generate_recording_attempts(
        monkeypatch, candidates, context, _pick("헥사펩타이드-2", "수국잎추출물")
    )

    assert calls == 1


async def test_concern_without_candidates_is_not_required(monkeypatch: pytest.MonkeyPatch):
    """근거 없는 고민까지 커버를 요구하면 뽑을 후보가 없어 재생성만 반복한다.

    그 상태는 ⑩의 `advisory.partial_evidence` 가 따로 알린다 (설계 04 §3-2).
    """
    candidates = [_c("헥사펩타이드-2", "brightening"), _c("레조시놀", "brightening")]
    context = UserContext(user_id="u", concerns=["redness", "brightening"])

    calls = await _generate_recording_attempts(
        monkeypatch, candidates, context, _pick("헥사펩타이드-2", "레조시놀")
    )

    assert calls == 1


# ── ⑥ 프롬프트 결정성 (설계 04 §9) ──────────────────────────────


def test_evidence_order_is_total_not_arrival_order():
    """동점 근거의 순서가 DB 반환 순서를 물려받으면 프롬프트가 실행마다 달라진다.

    프롬프트가 달라지면 온도 0·고정 seed 로도 ⑥의 선택이 흔들린다.
    """
    candidate = Candidate(name_kor="레조시놀", score=0.72, source_doc_ids=["a", "b", "c"])
    chunks = [_chunk(doc_id) for doc_id in ("c", "a", "b")]

    ordered = s6_generation._relevant_chunks(chunks, [candidate])

    assert [c.source.doc_id for c in ordered] == ["a", "b", "c"]
