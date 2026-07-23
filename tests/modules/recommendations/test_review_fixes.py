"""리뷰에서 확인된 결함 수정 회귀 테스트.

- B1: s7 성분 조회 정규화 (더러운 이름의 경고·표시 소실)
- B4/B5: 기능성 배지·보유 성분 표시 배선
- B3: 환각 재생성 게이트를 모든 시도에 적용
- B10: Langfuse 장애가 생성을 죽이지 않음
- M3: get_supabase 동시 생성 1회 보장
"""

import asyncio

from app.modules.recommendations.pipeline import s6_generation as generation
from app.modules.recommendations.pipeline import s7_response as response_stage
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    IngredientWarning,
    LlmNarrative,
    RetrievedChunk,
    UserContext,
)


def _chunk(name_kr: str, **meta) -> RetrievedChunk:
    return RetrievedChunk(
        content=f"[성분] {name_kr}",
        score=0.9,
        source=ChunkSource(doc_id=f"eff_{name_kr}", title="t"),
        metadata={"name_kr": name_kr, "efficacy": "효능", **meta},
    )


def _context(**kwargs) -> UserContext:
    return UserContext(**{"user_id": "u1", "age": 32, "concerns": ["pores"], **kwargs})


# ── B1: 더러운 이름이어도 경고·표시가 붙는다 ────────────────────────────


def test_dirty_name_keeps_warnings_and_clean_display():
    """레티놀처럼 개행·괄호가 붙은 이름도 정규화 조회로 경고·표시가 유지돼야 한다."""
    # ④가 만든 후보는 정규화된 이름("레티놀")을 키로 갖고 경고가 붙어 있다.
    cand = Candidate(
        name_kor="레티놀", score=0.9,
        warnings=[IngredientWarning(type="임신수유주의", text="권고되지 않습니다")],
    )
    by_name = {cand.name_kor: cand}
    # 그러나 검색 청크의 name_kr 은 원본(개행·괄호 포함)이다.
    dirty_chunk = _chunk("레티놀\n(비타민 A)")

    ing = response_stage._ingredient(dirty_chunk, by_name, _context())

    assert ing.name_kor == "레티놀"  # 원문이 아닌 정규화된 표시명
    assert [w.type for w in ing.warnings] == ["임신수유주의"]  # 경고가 소실되지 않음


# ── B4: 기능성 고시 배지 ─────────────────────────────────────────────


def test_functional_notice_badge_is_attached():
    ing = response_stage._ingredient(_chunk("나이아신아마이드"), {}, _context())
    assert ing.badges == ["기능성고시_미백"]


def test_non_functional_ingredient_has_no_badge():
    ing = response_stage._ingredient(_chunk("정제수"), {}, _context())
    assert ing.badges == []


# ── B5: 보유 성분 표시 ───────────────────────────────────────────────


def test_owned_ingredient_is_flagged_with_products():
    context = _context(
        owned_ingredients=["판테놀"],
        owned_products_by_ingredient={"판테놀": ["OO 토너"]},
    )
    ing = response_stage._ingredient(_chunk("판테놀"), {}, context)

    assert ing.owned is True
    assert ing.owned_products == ["OO 토너"]


def test_unowned_ingredient_is_not_flagged():
    ing = response_stage._ingredient(_chunk("판테놀"), {}, _context())
    assert ing.owned is False
    assert ing.owned_products == []


# ── B3: 재생성 게이트 ────────────────────────────────────────────────


def _narr(recommendation: str = "판테놀 추천", names=("판테놀",)) -> LlmNarrative:
    return LlmNarrative(
        cause_analysis="원인", recommendation=recommendation, usage_guide="사용법",
        recommended_names=list(names),
    )


async def _run_generate(monkeypatch, sequence) -> LlmNarrative:
    """_call_gemini 가 sequence 를 순서대로 반환(예외면 raise)하도록 두고 generate 실행."""
    seq = list(sequence)

    async def _fake(prompt, context, candidates):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(generation, "_call_gemini", _fake)
    return await generation.generate(
        _context(), [Candidate(name_kor="판테놀", score=0.9)], []
    )


async def test_clean_result_returned_without_retry(monkeypatch):
    clean = _narr()
    out = await _run_generate(monkeypatch, [clean])
    assert out is clean


async def test_tainted_then_clean_uses_clean(monkeypatch):
    tainted = _narr(recommendation="이 성분은 여드름을 치료합니다")  # "치료" 금칙어
    clean = _narr()
    out = await _run_generate(monkeypatch, [tainted, clean])
    assert out is clean


async def test_both_attempts_tainted_returns_last_for_sanitize(monkeypatch):
    """두 시도 다 오염되면 마지막 결과를 반환 — s7 sanitize_claims 가 최종 방어선."""
    t1 = _narr(recommendation="완치됩니다")
    t2 = _narr(recommendation="질환을 치료합니다")
    out = await _run_generate(monkeypatch, [t1, t2])
    assert out is t2  # 마지막 시도 (attempt==0 게이트 버그였다면 t1 을 반환했을 것)


async def test_exception_then_clean_recovers(monkeypatch):
    clean = _narr()
    out = await _run_generate(monkeypatch, [RuntimeError("일시 오류"), clean])
    assert out is clean


# ── B10: Langfuse 장애 격리 ──────────────────────────────────────────


def test_safe_trace_swallows_langfuse_failure(monkeypatch):
    def _boom():
        raise RuntimeError("Langfuse 다운")

    monkeypatch.setattr(generation, "get_client", _boom)

    generation._safe_trace(output="아무거나")  # 예외가 새어 나오면 실패


# ── M3: get_supabase 동시 생성 1회 ──────────────────────────────────


async def test_get_supabase_creates_once_under_concurrency(monkeypatch):
    from app.core import supabase as sb

    created = []

    async def _fake_create(url, key):
        created.append(1)
        await asyncio.sleep(0)  # 경쟁을 노출하려 양보
        return object()

    monkeypatch.setattr(sb, "acreate_client", _fake_create)
    monkeypatch.setattr(sb, "_client", None)

    results = await asyncio.gather(*[sb.get_supabase() for _ in range(10)])

    assert len(created) == 1  # 락이 없으면 여러 번 생성된다
    assert all(r is results[0] for r in results)
