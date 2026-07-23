"""2차 재검토에서 확인된 결함 수정 회귀 테스트.

- R1: s7 근거 패널이 ⑤ 제거 성분을 배지 달고 재노출하던 것 + 중복 회수 제거
- s3 retrieve_for_concerns 부분 성공 생존 / 전체 실패 503 (미커버였던 오케스트레이션)
- rate limit 윈도우 만료 후 쿼터 회복
"""

import pytest
from fastapi import HTTPException

from app.modules.recommendations import rate_limit
from app.modules.recommendations.constants import RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_SECONDS
from app.modules.recommendations.pipeline import s3_retrieval
from app.modules.recommendations.pipeline import s10_response as response_stage
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    LlmNarrative,
    RetrievedChunk,
    UserContext,
)


def _context(**kwargs) -> UserContext:
    return UserContext(**{"user_id": "u1", "age": 32, "concerns": ["pores"], **kwargs})


def _eff_chunk(name_kor: str, doc_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        content=f"[성분] {name_kor}", score=0.9,
        source=ChunkSource(doc_id=doc_id, title="t"),
        metadata={"name_kor": name_kor, "efficacy": "효능"},
    )


def _narr() -> LlmNarrative:
    return LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=["판테놀"],
    )


# ── R1: ⑤ 제거 성분은 근거 패널에서도 빠진다 ──────────────────────────


def test_safety_removed_ingredient_not_in_evidence_panel():
    """임신 금기로 ⑤가 제거한 레티놀이 배지·무경고로 근거 패널에 되살아나면 안 된다."""
    safe = [Candidate(name_kor="판테놀", score=0.9)]  # 레티놀은 ⑤에서 제거됨(safe 에 없음)
    efficacy_chunks = [_eff_chunk("판테놀", "eff_1"), _eff_chunk("레티놀", "eff_2")]

    resp = response_stage.assemble(_context(), safe, _narr(), [], efficacy_chunks, [])

    names = [i.name_kor for i in resp.ingredients]
    assert names == ["판테놀"]  # 레티놀은 없다
    assert all(i.name_kor != "레티놀" for i in resp.ingredients)
    # 레티놀이 기능성고시_주름개선 배지를 달고 새어나오지 않았는지
    assert not any("주름개선" in b for i in resp.ingredients for b in i.badges)


def test_duplicate_efficacy_chunks_are_deduped():
    """같은 성분이 여러 고민에서 두 번 회수돼도 근거 패널엔 1회만."""
    safe = [Candidate(name_kor="판테놀", score=0.9)]
    dupes = [_eff_chunk("판테놀", "eff_1"), _eff_chunk("판테놀", "eff_1")]

    resp = response_stage.assemble(_context(), safe, _narr(), [], dupes, [])

    assert [i.name_kor for i in resp.ingredients] == ["판테놀"]


def test_duplicate_cases_are_deduped():
    def _case_chunk(doc_id: str) -> RetrievedChunk:
        return RetrievedChunk(
            content="[상담]", score=0.9, source=ChunkSource(doc_id=doc_id, title="사례"),
            metadata={"recommended_ingredients": ["판테놀"], "target_concern": "모공"},
        )

    resp = response_stage.assemble(
        _context(), [Candidate(name_kor="판테놀", score=0.9)], _narr(),
        [_case_chunk("case_1"), _case_chunk("case_1")], [], [],
    )

    assert [c.id for c in resp.cases] == ["case_1"]


# ── s3 retrieve_for_concerns 부분/전체 실패 ─────────────────────────


async def test_partial_failure_keeps_surviving_concern(monkeypatch):
    """고민 하나가 실패해도 다른 고민의 근거는 살아남아야 한다 (확보한 근거 보존)."""
    async def _one(code, query):
        if code == "pores":
            raise s3_retrieval.RetrievalError("pores 검색 실패")
        return code, [_eff_chunk("사례", "case_x")], [_eff_chunk("판테놀", "eff_x")]

    monkeypatch.setattr(s3_retrieval, "_retrieve_one", _one)

    cases, efficacy = await s3_retrieval.retrieve_for_concerns(
        [("pores", "q1"), ("acne", "q2")]
    )

    assert efficacy and efficacy[0].metadata["concern"] == "acne"  # acne 생존


async def test_total_failure_raises_503(monkeypatch):
    async def _one(code, query):
        raise s3_retrieval.RetrievalError("전부 실패")

    monkeypatch.setattr(s3_retrieval, "_retrieve_one", _one)

    with pytest.raises(HTTPException) as exc_info:
        await s3_retrieval.retrieve_for_concerns([("pores", "q1"), ("acne", "q2")])
    assert exc_info.value.status_code == 503


# ── s3 _retrieve_one: leg 단위 부분 실패 (한 leg 만 embed/RPC 장애) ────
#
# 위 두 테스트는 _retrieve_one 전체를 patch 해 고민 단위 실패만 본다. leg 단위 부분
# 실패는 _retrieve_one 안에서 갈리므로, leg 인 retrieve() 를 collection 별로 성공/실패
# 시켜 실제 gather 분기를 태운다.


def _leg_chunk(doc_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        content="근거", score=0.9, source=ChunkSource(doc_id=doc_id, title="t"), metadata={}
    )


_CASES = s3_retrieval.RetrievalCollection.REC_CASES
_EFFICACY = s3_retrieval.RetrievalCollection.REC_EFFICACY


def _patch_legs(monkeypatch, fail: set) -> None:
    async def _retrieve(collection, query_text, top_k=5):
        if collection in fail:
            raise s3_retrieval.RetrievalError(f"{collection.value} 강제 실패")
        return [_leg_chunk(collection.value)]

    monkeypatch.setattr(s3_retrieval, "retrieve", _retrieve)


async def test_one_leg_failure_keeps_other_leg(monkeypatch):
    """efficacy leg 가 죽어도 같은 고민의 cases leg 근거는 살아남는다."""
    _patch_legs(monkeypatch, fail={_EFFICACY})

    cases, efficacy = await s3_retrieval.retrieve_for_concerns([("pores", "q1")])

    assert [c.source.doc_id for c in cases] == ["rec_cases"], "성공한 leg 는 버려지지 않는다"
    assert efficacy == [], "실패한 leg 는 빈 목록"


async def test_both_legs_failing_for_only_concern_raises_503(monkeypatch):
    """유일한 고민의 두 leg 가 모두 실패하면 전부 실패 → 503."""
    _patch_legs(monkeypatch, fail={_CASES, _EFFICACY})

    with pytest.raises(HTTPException) as exc_info:
        await s3_retrieval.retrieve_for_concerns([("pores", "q1")])
    assert exc_info.value.status_code == 503


async def test_leg_failure_on_one_collection_survives_across_concerns(monkeypatch):
    """cases leg 가 모든 고민에서 죽어도 efficacy leg 는 고민별로 살아남는다 (503 아님)."""
    _patch_legs(monkeypatch, fail={_CASES})

    cases, efficacy = await s3_retrieval.retrieve_for_concerns([("pores", "q1"), ("acne", "q2")])

    assert cases == [], "cases leg 는 두 고민 모두 실패"
    assert len(efficacy) == 2, "efficacy leg 는 두 고민 모두 생존"


# ── rate limit 윈도우 회복 ───────────────────────────────────────────


def test_rate_limit_recovers_after_window(monkeypatch):
    rate_limit._reset()
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])

    for _ in range(RATE_LIMIT_MAX):
        rate_limit.enforce_rate_limit("u")
    with pytest.raises(HTTPException):  # 상한 초과
        rate_limit.enforce_rate_limit("u")

    clock[0] += RATE_LIMIT_WINDOW_SECONDS + 1  # 윈도우 밖으로 진전
    rate_limit.enforce_rate_limit("u")  # 쿼터 회복 — 예외 없음
    rate_limit._reset()
