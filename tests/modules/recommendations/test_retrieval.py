"""retrieval 테스트 — 청크 매핑·score·fallback·예외 감싸기.

키워드 대체 검색 모드의 계약을 고정한다. 서지우의 벡터 검색으로 교체할 때 이 테스트가
계약(0~1 score·빈 목록·metadata 키)이 지켜지는지 확인하는 기준이 된다.
"""

import pytest

from app.modules.recommendations.pipeline import s3_retrieval as retrieval
from app.modules.recommendations.pipeline.s3_retrieval import (
    RetrievalCollection,
    RetrievalError,
    retrieve,
)
from tests.modules.recommendations.conftest import FakeSupabase

_CASE_ROW = {
    "case_id": "COT_POR_F_O30_00001",
    "target_concern": "모공",
    "skin_concerns": ["모공", "여드름/뾰루지"],
    "question": "모공이 넓어졌어요",
    "answer": "나이아신아마이드를 권합니다",
    "cot": ["1단계", "2단계 근거", "3단계"],
    "recommended_ingredients": ["나이아신아마이드"],
    "evidence_sources": ["PMID:29061803"],
    "gender": "여성",
    "age": 32,
}

_EFFICACY_ROW = {
    "id": 42,
    "inci": "NIACINAMIDE",
    "name_kr": "나이아신아마이드",
    "efficacy": "피지 조절",
    "safety_note": "고농도 자극 가능",
    "recommended_concentration": "2~5%",
    "recommended_skin_types": "지성",
    "regulation_note": "배합 한도 있음",
    "reference_source": "PMID:29061803",
    "ingredient_id": 1234,
}


def _patch(monkeypatch: pytest.MonkeyPatch, tables=None, missing=None) -> None:
    async def _fake() -> FakeSupabase:
        return FakeSupabase(tables, missing)

    monkeypatch.setattr(retrieval, "get_supabase", _fake)


# ── score 계약 ────────────────────────────────────────────────


def test_rank_score_decreases_and_has_floor():
    scores = [retrieval._rank_score(i) for i in range(20)]

    assert scores[0] > scores[1] > scores[2]
    assert all(0.0 <= s <= 1.0 for s in scores), "score 는 0~1 로 정규화된다"
    assert min(scores) == retrieval._FAKE_MIN_SCORE


# ── 청크 매핑 ─────────────────────────────────────────────────


async def test_case_chunk_mapping(monkeypatch):
    _patch(monkeypatch, {"rec_cases": [_CASE_ROW]})

    chunks = await retrieve(RetrievalCollection.REC_CASES, "모공", {"concern_labels": ["모공"]}, 3)

    chunk = chunks[0]
    assert chunk.source.doc_id == "COT_POR_F_O30_00001"
    assert chunk.source.locator == "PMID:29061803"
    assert "모공이 넓어졌어요" in chunk.content
    assert chunk.metadata["recommended_ingredients"] == ["나이아신아마이드"]


async def test_efficacy_chunk_carries_safety_metadata(monkeypatch):
    """⑤ 안전성 필터가 소비하는 키가 metadata 에 실려야 한다 (02 §4 계약)."""
    _patch(monkeypatch, {"rec_efficacy": [_EFFICACY_ROW]})

    chunks = await retrieve(RetrievalCollection.REC_EFFICACY, "모공", {"keywords": ["모공"]}, 5)

    meta = chunks[0].metadata
    assert meta["ingredient_id"] == 1234
    assert meta["safety_note"] == "고농도 자극 가능"
    assert meta["recommended_concentration"] == "2~5%"
    assert meta["regulation_note"] == "배합 한도 있음"
    assert meta["recommended_skin_types"] == "지성"


async def test_efficacy_chunk_falls_back_to_inci_when_no_korean_name(monkeypatch):
    _patch(monkeypatch, {"rec_efficacy": [{**_EFFICACY_ROW, "name_kr": None}]})

    chunks = await retrieve(RetrievalCollection.REC_EFFICACY, "모공", {"keywords": ["모공"]}, 5)

    assert "NIACINAMIDE" in chunks[0].content


async def test_cot_non_string_steps_do_not_leak_repr(monkeypatch):
    """cot 원소가 dict 면 근거 텍스트에 {'step': ...} repr 이 그대로 들어가 프롬프트로 간다."""
    _patch(monkeypatch, {"rec_cases": [{**_CASE_ROW, "cot": [{"step": 1}, {"step": 2}]}]})

    chunks = await retrieve(RetrievalCollection.REC_CASES, "모공", {}, 3)

    assert "{'step'" not in chunks[0].content


# ── 빈 결과·fallback·예외 ──────────────────────────────────────


async def test_empty_result_is_empty_list_not_error(monkeypatch):
    """결과 없음은 예외가 아니라 빈 목록이다 (호출자가 근거 없음을 판단한다)."""
    _patch(monkeypatch, {"rec_cases": []})

    assert await retrieve(RetrievalCollection.REC_CASES, "모공", {"concern_labels": ["모공"]}) == []


async def test_infrastructure_failure_wrapped_in_retrieval_error(monkeypatch):
    _patch(monkeypatch, missing={"rec_efficacy"})

    with pytest.raises(RetrievalError):
        await retrieve(RetrievalCollection.REC_EFFICACY, "모공", {"keywords": ["모공"]})


async def test_sparse_concern_falls_back_to_unfiltered_search(monkeypatch):
    """겹침 매칭이 0건인 희소 고민(민감성 6건)에서 무필터로 한 번 더 찾는다.

    FakeQuery 는 필터를 무시하므로, 겹침 결과가 비었을 때 fallback 이 호출되는지를
    호출 횟수로 확인한다.
    """
    calls: list[str] = []
    executes = {"n": 0}

    class _CountingQuery:
        def __init__(self, rows):
            self._rows = rows

        def __getattr__(self, name):
            def _chain(*_, **__):
                calls.append(name)
                return self

            return _chain

        async def execute(self):
            executes["n"] += 1
            # 1차(겹침 매칭)는 0건, 2차(무필터 fallback)는 1건
            rows = [] if executes["n"] == 1 else self._rows
            return type("Result", (), {"data": rows})()

    class _Client:
        def table(self, _name):
            return _CountingQuery([_CASE_ROW])

    async def _fake():
        return _Client()

    monkeypatch.setattr(retrieval, "get_supabase", _fake)

    chunks = await retrieve(
        RetrievalCollection.REC_CASES, "민감성", {"concern_labels": ["민감성(트러블/자극감)"]}, 3
    )

    assert "overlaps" in calls, "1차는 배열 겹침 매칭"
    assert chunks, "겹침이 비면 무필터 fallback 이 결과를 채운다"
    # fallback 행은 고민과의 관련성이 검증되지 않았다. 일반 경로와 같은 score 를 주면
    # 관련 있는 근거를 밀어내고 프롬프트 앞자리를 차지한다 (근거 기반 생성 규칙).
    assert all(c.score == retrieval._FALLBACK_SCORE for c in chunks)
    assert retrieval._rank_score(0) > retrieval._FALLBACK_SCORE


async def test_matched_cases_keep_rank_score(monkeypatch):
    """겹침이 잡힌 정상 경로까지 fallback 점수로 떨어지면 안 된다."""
    _patch(monkeypatch, {"rec_cases": [_CASE_ROW]})

    chunks = await retrieve(RetrievalCollection.REC_CASES, "모공", {"concern_labels": ["모공"]}, 3)

    assert chunks[0].score == retrieval._rank_score(0)
