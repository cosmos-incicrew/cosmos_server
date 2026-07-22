"""추천 파이프라인 단위 테스트 — ④ 집계 · ⑤ 안전성 필터 · ⑥ 환각 차단 · ⑦ 조립.

DB·Gemini 는 부르지 않는다. 순수 로직만 떼어 검증한다.
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.modules.recommendations import bsti_traits
from app.modules.recommendations.constants import (
    BSTI_BOOST,
    MAX_CANDIDATES,
    MAX_CHUNK_CHARS,
    MAX_EVIDENCE_CHARS,
    OWNED_PENALTY,
    PREGNANCY_AVOID,
)
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.recommendations.pipeline import s2_queries as queries_stage
from app.modules.recommendations.pipeline import s4_candidates as candidates_stage
from app.modules.recommendations.pipeline import s5_safety as safety
from app.modules.recommendations.pipeline import s6_generation as generation
from app.modules.recommendations.pipeline import s7_response as response_stage
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    LlmOutput,
    LlmPick,
    RetrievedChunk,
    UserContext,
)


def _efficacy_chunk(name: str, score: float, concern: str = "pores", **meta) -> RetrievedChunk:
    return RetrievedChunk(
        content=f"[성분] {name}",
        score=score,
        source=ChunkSource(doc_id=f"eff_{name}", title=f"{name} 효능"),
        metadata={"name_kr": name, "efficacy": f"{name} 효능", "concern": concern, **meta},
    )


def _case_chunk(names: list[str], score: float, concern: str = "pores") -> RetrievedChunk:
    return RetrievedChunk(
        content="[상담] ...",
        score=score,
        source=ChunkSource(doc_id="case_1", title="상담 사례"),
        metadata={"recommended_ingredients": names, "concern": concern},
    )


def _context(**kwargs) -> UserContext:
    base = {"user_id": "u1", "age": 32, "gender": "female", "concerns": ["pores"]}
    return UserContext(**{**base, **kwargs})


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch):
    """④의 ingredient_id 조인은 DB를 타므로 테스트에서는 건너뛴다."""

    async def _noop(candidates):
        return None

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _noop)


# ── BSTI 축 해석 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("OSPW", "지성·민감·색소침착 경향·주름 경향 피부"),
        ("DRNT", "건성·저항·색소 안정·탱탱함 유지 피부"),
        (None, ""),
        ("XX", ""),
    ],
)
def test_bsti_describe(code, expected):
    assert bsti_traits.describe(code) == expected


def test_bsti_sensitive_axis():
    assert bsti_traits.is_sensitive("OSPW") is True
    assert bsti_traits.is_sensitive("ORPW") is False


# ── ④ 후보 집계 ───────────────────────────────────────────────


async def test_dedupe_keeps_highest_score_and_unions_concerns():
    chunks = [
        _efficacy_chunk("나이아신아마이드", 0.7, concern="pores"),
        _efficacy_chunk("나이아신아마이드", 0.9, concern="brightening"),
    ]

    candidates = await candidates_stage.aggregate_candidates([], chunks, _context())

    assert len(candidates) == 1
    assert candidates[0].score == pytest.approx(0.9)
    assert set(candidates[0].concerns) == {"pores", "brightening"}


async def test_bsti_boost_applied_once():
    """가점·감점을 한 테스트에서 합치면 BSTI_BOOST == OWNED_PENALTY 라 서로 상쇄돼
    두 로직을 다 지워도 통과한다. 반드시 따로 검증한다."""
    chunks = [_efficacy_chunk("판테놀", 0.8), _efficacy_chunk("판테놀", 0.6)]

    candidates = await candidates_stage.aggregate_candidates(
        [], chunks, _context(bsti_recommended=["판테놀"])
    )

    # 청크 2개여도 가점은 dedupe 이후 1회만
    assert candidates[0].score == pytest.approx(0.8 + BSTI_BOOST)


async def test_owned_penalty_applied_once():
    chunks = [_efficacy_chunk("판테놀", 0.8), _efficacy_chunk("판테놀", 0.6)]

    candidates = await candidates_stage.aggregate_candidates(
        [], chunks, _context(owned_ingredients=["판테놀"])
    )

    assert candidates[0].score == pytest.approx(0.8 - OWNED_PENALTY)


async def test_case_ingredients_merge_into_candidates():
    candidates = await candidates_stage.aggregate_candidates(
        [_case_chunk(["세라마이드"], 0.85)], [_efficacy_chunk("판테놀", 0.8)], _context()
    )

    assert {c.name_kor for c in candidates} == {"세라마이드", "판테놀"}


async def test_candidate_cap_enforced():
    chunks = [_efficacy_chunk(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_CANDIDATES + 5)]

    candidates = await candidates_stage.aggregate_candidates([], chunks, _context())

    assert len(candidates) == MAX_CANDIDATES


# ── ⑤ 안전성 필터 ─────────────────────────────────────────────


@pytest.fixture()
def _no_restrictions(monkeypatch: pytest.MonkeyPatch):
    """조회는 성공했고 해당 성분에 규제가 없는 상태 (ok=True)."""

    async def _empty(candidates):
        return safety.RestrictionLookup({}, {}, ok=True)

    monkeypatch.setattr(safety, "fetch_restrictions", _empty)


async def test_pregnancy_contraindicated_removed_when_expecting(_no_restrictions):
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context(is_pregnant=True))

    assert kept == []


async def test_pregnancy_unknown_warns_instead_of_removing(_no_restrictions):
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context())

    assert len(kept) == 1  # 미수집 상태의 일괄 제거는 과차단이라 경고만
    assert any(w.type == "임신수유주의" for w in kept[0].warnings)


async def test_banned_restriction_removes_candidate(monkeypatch: pytest.MonkeyPatch):
    async def _banned(candidates):
        return safety.RestrictionLookup(
            {1: {"regulate_type": "금지", "ingredient_id": 1}}, {}, ok=True
        )

    monkeypatch.setattr(safety, "fetch_restrictions", _banned)
    candidates = [Candidate(name_kor="금지성분", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context())

    assert kept == []


async def test_unmapped_candidate_gets_safety_unknown_warning(_no_restrictions):
    candidates = [Candidate(name_kor="미매핑성분", score=0.9, ingredient_id=None)]

    kept = await safety.apply_safety_filters(candidates, _context())

    assert any(w.type == "안전성확인불가" for w in kept[0].warnings)


async def test_allergen_warning_emphasized_for_sensitive_type(_no_restrictions):
    candidates = [Candidate(name_kor="리날룰", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context(bsti_type="OSPW"))

    warning = next(w for w in kept[0].warnings if w.type == "알레르기유발")
    assert "첩포" in warning.text


async def test_concern_conflict_warning(_no_restrictions):
    candidates = [
        Candidate(
            name_kor="살리실릭애씨드2", score=0.9, ingredient_id=1, recommended_skin_types="지성"
        )
    ]

    kept = await safety.apply_safety_filters(candidates, _context(concerns=["pores", "dryness"]))

    assert any(w.type == "고민상충" for w in kept[0].warnings)


# ── ⑥ 환각 차단 · 표현 규제 ────────────────────────────────────


def test_picks_outside_candidates_are_dropped():
    candidates = [Candidate(name_kor="판테놀", score=0.9)]
    picks = [
        LlmPick(name_kor="판테놀", reason="진정에 도움"),
        LlmPick(name_kor="존재하지않는성분", reason="지어낸 성분"),
    ]

    assert [p.name_kor for p in generation._reject_hallucinated(picks, candidates)] == ["판테놀"]


def test_banned_claim_sentence_removed():
    text = "여드름을 치료해 줍니다. 피부 진정에 도움이 됩니다."

    assert generation.sanitize_claims(text) == "피부 진정에 도움이 됩니다."


def test_all_sentences_banned_falls_back_to_safe_text():
    assert "근거" in generation.sanitize_claims("질환을 완치합니다.")


# ── ⑦ 응답 조립 ───────────────────────────────────────────────


def test_assemble_attaches_badge_owned_and_sources():
    candidate = Candidate(
        name_kor="나이아신아마이드",
        score=0.9,
        ingredient_id=42,
        efficacy="피지 조절",
        concerns=["pores"],
        source_doc_ids=["eff_1"],
    )
    chunk = RetrievedChunk(
        content="근거",
        score=0.9,
        source=ChunkSource(doc_id="eff_1", title="효능", locator="PMID:1"),
        metadata={},
    )
    picks = [
        LlmPick(name_kor="나이아신아마이드", reason="모공 관리에 도움", cited_doc_ids=["eff_1"])
    ]
    context = _context(
        owned_ingredients=["나이아신아마이드"],
        owned_products_by_ingredient={"나이아신아마이드": ["OO 토너"]},
    )

    response = response_stage.assemble(context, [candidate], picks, [chunk])

    item = response.recommended_ingredients[0]
    assert response.status == "ok"
    assert item.badges == ["기능성고시_미백"]  # LLM 이 아니라 코드가 붙인다
    assert item.owned is True
    assert item.owned_products == ["OO 토너"]
    assert item.sources[0].locator == "PMID:1"
    assert response.recommended_products == []  # v1은 항상 빈 배열


def test_assemble_without_usable_picks_returns_insufficient():
    response = response_stage.assemble(_context(), [], [], [])

    assert response.status == "insufficient_evidence"
    assert response.recommended_ingredients == []


def test_insufficient_suggests_bsti_when_not_taken():
    assert response_stage.insufficient(_context(), "없음").suggested_action == "take_bsti"
    assert (
        response_stage.insufficient(_context(bsti_type="OSPW"), "없음").suggested_action
        == "retry_with_other_concerns"
    )


# ── ② 질의 구성 ───────────────────────────────────────────────


def test_query_includes_age_gender_and_skin_traits():
    queries = queries_stage.build_queries(_context(bsti_type="OSPW"))

    assert len(queries) == 1
    code, query = queries[0]
    assert code == "pores"
    assert "30대" in query and "여성" in query and "지성·민감" in query and "모공" in query


def test_query_omits_missing_elements():
    _, query = queries_stage.build_queries(_context(gender=None, bsti_type=None))[0]

    assert "여성" not in query and "피부의" not in query


# ── 성분명 정규화 (실 데이터 회귀 방지) ─────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("레티놀\n(비타민 A)", "레티놀"),  # rec_efficacy id=1756 실제 값
        ("레틴알(레티날)", "레틴알"),  # id=1755
        (
            "비스-에칠헥실옥시페놀메톡시페닐트리아진\n[베모트리지놀]",
            "비스-에칠헥실옥시페놀메톡시페닐트리아진",
        ),
        ("나이아신아마이드", "나이아신아마이드"),
        ("  판테놀  ", "판테놀"),
    ],
)
def test_normalize_ingredient_name(raw, expected):
    assert normalize_ingredient_name(raw) == expected


def test_normalize_does_not_false_match_non_retinoids():
    """`플로레틴`·`글리시레티닉애씨드`는 "레티/레틴"을 포함하지만 레티노이드가 아니다.
    부분 문자열 매칭으로 바꾸면 이 둘이 임신 금기로 오탐된다."""
    for name in ("플로레틴", "글리시레티닉애씨드"):
        assert normalize_ingredient_name(name) not in PREGNANCY_AVOID


async def test_newline_retinol_is_removed_for_pregnant_user(_no_restrictions):
    """실 DB 의 `"레티놀\\n(비타민 A)"` 가 임신 금기 필터를 통과하던 회귀를 막는다."""
    candidates = [Candidate(name_kor="레티놀\n(비타민 A)", score=0.9, ingredient_id=None)]

    kept = await safety.apply_safety_filters(candidates, _context(is_pregnant=True))

    assert kept == [], "정규화 없이 원본 문자열로 조회하면 임신부에게 레티놀이 추천된다"


async def test_newline_name_normalized_at_merge(_no_restrictions):
    candidates = await candidates_stage.aggregate_candidates(
        [], [_efficacy_chunk("레티놀\n(비타민 A)", 0.9)], _context()
    )

    assert candidates[0].name_kor == "레티놀"


async def test_pregnancy_partially_collected_still_warns(_no_restrictions):
    """is_pregnant=False, is_nursing=None 은 제거도 경고도 안 되던 구멍이었다."""
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(
        candidates, _context(is_pregnant=False, is_nursing=None)
    )

    assert any(w.type == "임신수유주의" for w in kept[0].warnings)


# ── 사용제한 조회 (fail-open · 금지 우선) ───────────────────────


async def test_restriction_lookup_failure_warns_every_candidate(monkeypatch):
    """조회 실패를 "제한 없음"으로 읽으면 금지 성분이 무경고로 나간다."""

    async def _failed(candidates):
        return safety.RestrictionLookup({}, {}, ok=False)

    monkeypatch.setattr(safety, "fetch_restrictions", _failed)
    candidates = [Candidate(name_kor="어떤성분", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context())

    assert any(w.type == "안전성확인불가" for w in kept[0].warnings)


def test_worst_restriction_prefers_ban_over_limit():
    """한 성분에 금지·한도 행이 함께 있으면 금지가 이겨야 한다 (dict 덮어쓰기 회귀)."""
    rows = [
        {"regulate_type": "한도", "limit_cond": "3% 이하"},
        {"regulate_type": "금지"},
    ]

    assert safety._worst(rows)["regulate_type"] == "금지"
    assert safety._worst(list(reversed(rows)))["regulate_type"] == "금지"


async def test_inci_and_korean_candidate_collapse_after_id_mapping(monkeypatch):
    """상담 사례에 한글명과 INCI 가 함께 들어 있으면 매핑 후 같은 성분이 된다.
    합치지 않으면 후보 슬롯을 두 개 먹고 응답에도 두 번 나온다."""

    async def _resolve(candidates):
        for c in candidates:
            if c.name_kor == "Hexapeptide-2":
                c.ingredient_id = 4871
                c.name_kor = "헥사펩타이드-2"
            elif c.name_kor == "헥사펩타이드-2":
                c.ingredient_id = 4871

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _resolve)
    chunks = [
        _case_chunk(["헥사펩타이드-2"], 0.9, concern="wrinkles"),
        _case_chunk(["Hexapeptide-2"], 0.85, concern="pores"),
    ]

    candidates = await candidates_stage.aggregate_candidates(chunks, [], _context())

    assert len(candidates) == 1
    assert candidates[0].name_kor == "헥사펩타이드-2"
    assert set(candidates[0].concerns) == {"wrinkles", "pores"}


async def test_notes_collapse_into_single_warning(_no_restrictions):
    candidates = [
        Candidate(
            name_kor="성분A",
            score=0.9,
            ingredient_id=1,
            safety_note="자극 가능",
            recommended_concentration="2% 이하",
            regulation_note="배합 한도 있음",
        )
    ]

    kept = await safety.apply_safety_filters(candidates, _context())

    notes = [w for w in kept[0].warnings if w.type == "주의사항"]
    assert len(notes) == 1
    assert "자극 가능" in notes[0].text and "2% 이하" in notes[0].text


# ── 명시 시나리오 (설계 §5) ────────────────────────────────────


async def test_sparse_concern_user_gets_insufficient_evidence():
    """민감성 단독 고민(사례 6건)은 확인 불가를 가장 자주 만나는 집단이다.
    근거가 없으면 LLM 을 부르지 않고 정형 응답 + 행동 유도를 돌려줘야 한다."""
    context = _context(concerns=["sensitivity"], bsti_type=None)

    response = response_stage.insufficient(context, "근거 없음")

    assert response.status == "insufficient_evidence"
    assert response.suggested_action == "take_bsti"  # BSTI 미검사 → 검사 유도
    assert response.recommended_ingredients == []
    assert response.context_used.concerns == ["sensitivity"]


async def test_empty_shelf_skips_only_owned_adjustments():
    """화장대가 비어도 추천은 정상 진행되고 하향·보유 표시만 생략된다."""
    chunks = [_efficacy_chunk("판테놀", 0.8)]

    candidates = await candidates_stage.aggregate_candidates(
        [], chunks, _context(owned_ingredients=[])
    )

    assert candidates[0].score == pytest.approx(0.8), "빈 화장대에서는 감점이 없다"

    response = response_stage.assemble(
        _context(owned_ingredients=[]),
        candidates,
        [LlmPick(name_kor="판테놀", reason="진정에 도움")],
        [],
    )

    item = response.recommended_ingredients[0]
    assert response.status == "ok"
    assert item.owned is False
    assert item.owned_products == []


# ── ⑥ 재생성·에러 트레이스 ─────────────────────────────────────


async def test_banned_claim_triggers_one_regeneration(monkeypatch):
    """금칙어가 섞이면 문장 삭제로 끝내지 않고 1회 재생성한다 (설계 §2-⑥ "순화·재생성")."""
    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        reason = "여드름을 치료합니다." if calls["n"] == 1 else "피부 진정에 도움이 됩니다."
        return LlmOutput(picks=[LlmPick(name_kor="판테놀", reason=reason)])

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)

    picks = await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert calls["n"] == 2, "1회 재생성해야 한다"
    assert "치료" not in picks[0].reason


async def test_clean_generation_does_not_regenerate(monkeypatch):
    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        return LlmOutput(picks=[LlmPick(name_kor="판테놀", reason="피부 진정에 도움이 됩니다.")])

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)

    await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert calls["n"] == 1


async def test_llm_failure_raises_502_after_one_retry(monkeypatch):
    calls = {"n": 0}

    async def _always_fails(prompt, context, candidates):
        calls["n"] += 1
        raise RuntimeError("upstream down")

    monkeypatch.setattr(generation, "_call_gemini", _always_fails)

    with pytest.raises(HTTPException) as exc:
        await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "LLM_UPSTREAM_ERROR"
    assert calls["n"] == 2


def test_candidate_block_keeps_one_line_per_candidate():
    """efficacy 원문의 개행이 불릿을 쪼개면 LLM 이 후보 경계를 잘못 읽는다."""
    candidates = [
        Candidate(name_kor="성분A", score=0.9, efficacy="미백\n노화방지 및 재생"),
        Candidate(name_kor="성분B", score=0.8),
    ]

    block = generation._candidate_block(candidates)

    assert len(block.splitlines()) == 2


async def test_placeholder_notes_are_dropped(_no_restrictions):
    """데이터의 "없음" 자리채움이 경고 문구로 새어 나가던 것."""
    candidates = [
        Candidate(
            name_kor="성분A",
            score=0.9,
            ingredient_id=1,
            safety_note="자극 가능",
            regulation_note="없음",
        )
    ]

    kept = await safety.apply_safety_filters(candidates, _context())

    note = next(w for w in kept[0].warnings if w.type == "주의사항")
    assert "없음" not in note.text


async def test_regeneration_failure_keeps_first_result(monkeypatch):
    """금칙어 재생성 중 통신 오류가 나면 1차 결과를 살린다.

    ⑦의 순화로 처리 가능한 문제인데 502 로 버리면, 일시적 오류가 멀쩡한 추천을 죽인다.
    """
    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        if calls["n"] == 1:
            return LlmOutput(picks=[LlmPick(name_kor="판테놀", reason="여드름을 치료합니다.")])
        raise ConnectionError("일시적 오류")

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)

    picks = await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert calls["n"] == 2
    assert [p.name_kor for p in picks] == ["판테놀"], "1차 결과를 502 로 버리면 안 된다"
    assert "치료" not in generation.sanitize_claims(picks[0].reason)


async def test_first_attempt_failure_still_raises_502(monkeypatch):
    """살릴 결과가 아예 없을 때는 기존대로 502."""

    async def _always_fails(prompt, context, candidates):
        raise ConnectionError("계속 실패")

    monkeypatch.setattr(generation, "_call_gemini", _always_fails)

    with pytest.raises(HTTPException) as exc:
        await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert exc.value.status_code == 502


async def test_candidate_cap_applied_after_dedupe(monkeypatch):
    """중복 제거 전에 자르면 합쳐진 만큼 후보가 상한 아래로 떨어진다."""

    async def _resolve(cands):
        # 앞의 두 개를 같은 성분으로 매핑 — 중복 제거 대상이 된다
        for c in cands:
            if c.name_kor in ("성분0", "성분1"):
                c.ingredient_id = 999
                c.name_kor = "합쳐진성분"

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _resolve)
    chunks = [_efficacy_chunk(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_CANDIDATES + 5)]

    result = await candidates_stage.aggregate_candidates([], chunks, _context())

    assert len(result) == MAX_CANDIDATES, "중복으로 빈 자리는 다음 후보가 채워야 한다"
    assert len({c.name_kor for c in result}) == MAX_CANDIDATES


async def test_dedupe_key_handles_zero_ingredient_id(monkeypatch):
    """ingredient_id 가 0 인 두 이명이 합쳐져야 한다.

    키를 `ingredient_id or name_kor` 로 잡으면 0 이 falsy 라 둘 다 이름 키로 새고,
    같은 성분이 후보에 두 번 남는다.
    """

    async def _resolve(cands):
        for c in cands:
            c.ingredient_id = 0  # 같은 성분의 서로 다른 표기

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _resolve)
    chunks = [_efficacy_chunk("성분A", 0.9), _efficacy_chunk("성분A-이명", 0.8)]

    result = await candidates_stage.aggregate_candidates([], chunks, _context())

    assert len(result) == 1, "같은 ingredient_id 는 표기가 달라도 한 성분이다"
    assert result[0].ingredient_id == 0


# ── ⑥ 프롬프트 분량·프라이버시 ─────────────────────────────────


def _chunk_of(doc_id: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        content=content, score=0.9, source=ChunkSource(doc_id=doc_id, title="t"), metadata={}
    )


async def test_evidence_drops_chunks_unlinked_to_candidates(monkeypatch):
    """⑤에서 걸러진 성분의 근거는 프롬프트에 넣지 않는다 (고를 수 없는 성분 설명).

    헬퍼만 직접 부르면 `generate()` 안의 호출을 지워도 통과한다. 실제 프롬프트를
    잡아서 확인한다.
    """
    captured: dict = {}

    async def _fake_gemini(prompt, context, candidates):
        captured["prompt"] = prompt
        return LlmOutput(picks=[LlmPick(name_kor="판테놀", reason="도움이 됩니다.")])

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)
    candidates = [Candidate(name_kor="판테놀", score=0.9, source_doc_ids=["eff_1"])]
    chunks = [_chunk_of("eff_1", "살아있는 근거"), _chunk_of("eff_99", "걸러진 성분 근거")]

    await generation.generate(_context(), candidates, chunks)

    assert "살아있는 근거" in captured["prompt"]
    assert "걸러진 성분 근거" not in captured["prompt"]


def test_evidence_block_respects_total_cap():
    """상담 답변이 길어도 프롬프트가 무한정 커지지 않는다 (Pro 입력 비용)."""
    chunks = [_chunk_of(f"eff_{i}", "가" * 1000) for i in range(50)]

    block = generation._evidence_block(chunks)

    assert len(block) <= MAX_EVIDENCE_CHARS
    assert block, "상한이 있어도 최소 몇 건은 들어가야 한다"


def test_single_chunk_cannot_monopolize_evidence():
    chunks = [_chunk_of("eff_1", "가" * 9999), _chunk_of("eff_2", "짧은 근거")]

    block = generation._evidence_block(chunks)

    assert "eff_2" in block, "긴 청크 하나가 나머지를 밀어내면 안 된다"


def test_langfuse_input_omits_personal_attributes():
    """트레이스는 외부 SaaS 에 남는다 — 나이·성별·보유 성분이 그대로 가면 안 된다."""
    ctx = _context(age=32, gender="female", owned_ingredients=["판테놀"])
    prompt = f"지시문\n{generation._user_block(ctx)}\n끝"

    redacted = generation._redact_personal(prompt, ctx)

    assert "32" not in redacted
    assert "female" not in redacted and "여성" not in redacted
    assert "판테놀" not in redacted
    assert "지시문" in redacted, "프롬프트 본문은 남아야 디버깅이 된다"


# ── 임신·수유 2단계 (2026-07-20 근거 조사 반영) ─────────────────


async def test_salicylic_acid_is_warned_not_removed_when_pregnant(_no_restrictions):
    """살리실릭애씨드 일괄 제외는 과차단이다.

    MotherSafe(NSW Health)는 화장품 농도에서 임신 중 안전하다고 보고, EU SCCS 도
    농도 조건부로 허용하며 임부 제한을 부과하지 않는다. 제외하면 임신부에게서
    여드름·모공 성분이 통째로 사라진다.
    """
    candidates = [Candidate(name_kor="살리실릭애씨드", score=0.9, ingredient_id=812)]

    kept = await safety.apply_safety_filters(candidates, _context(is_pregnant=True))

    assert len(kept) == 1, "제외가 아니라 경고여야 한다"
    warning = next(w for w in kept[0].warnings if w.type == "임신수유주의")
    assert "함량" in warning.text


async def test_retinoid_still_removed_when_pregnant(_no_restrictions):
    """근거상 최기형성은 확인되지 않았지만 저자 결론이 '권고되지 않음'이라 제외 유지."""
    candidates = [Candidate(name_kor="레티닐팔미테이트", score=0.9, ingredient_id=5493)]

    kept = await safety.apply_safety_filters(candidates, _context(is_pregnant=True))

    assert kept == []


async def test_pregnancy_warning_text_says_not_advised_not_forbidden(_no_restrictions):
    """근거는 '금기'가 아니라 '권고되지 않음'이다 — 문구가 사실보다 세면 안 된다."""
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context())

    text = next(w.text for w in kept[0].warnings if w.type == "임신수유주의")
    assert "권고되지 않는" in text
    assert "금기" not in text


async def test_caution_ingredient_not_warned_when_not_pregnant(_no_restrictions):
    """임신·수유가 아님이 확인된 사용자에겐 임신 관련 경고를 띄우지 않는다."""
    candidates = [Candidate(name_kor="살리실릭애씨드", score=0.9, ingredient_id=812)]

    kept = await safety.apply_safety_filters(
        candidates, _context(is_pregnant=False, is_nursing=False)
    )

    assert not any(w.type == "임신수유주의" for w in kept[0].warnings)


# ── 2026-07-20 리뷰 반영 회귀 ──────────────────────────────────


async def test_weights_apply_to_renamed_inci_candidate(monkeypatch):
    """ID 매핑이 INCI 후보를 한글로 개명하므로 가중치는 그 뒤에 적용해야 한다.

    앞에서 적용하면 영문명으로 들어온 후보(상담 사례 성분의 40%)가 보유 하향을 못 받는데,
    ⑦의 보유 배지는 개명 후 이름으로 판정한다 — "보유 표시는 되는데 하향은 안 된" 후보.
    """

    async def _resolve(cands):
        for c in cands:
            if c.name_kor == "NIACINAMIDE":
                c.ingredient_id = 1234
                c.name_kor = "나이아신아마이드"

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _resolve)

    candidates = await candidates_stage.aggregate_candidates(
        [],
        [_efficacy_chunk("NIACINAMIDE", 0.9)],
        _context(owned_ingredients=["나이아신아마이드"]),
    )

    assert candidates[0].name_kor == "나이아신아마이드"
    assert candidates[0].score == pytest.approx(0.9 - OWNED_PENALTY)


async def test_evidence_sorted_by_score_before_truncation(monkeypatch):
    """분량 절단은 관련성 낮은 것부터여야 한다.

    ⑥에 들어오는 청크는 `case_chunks + efficacy_chunks` 라 고민 순서다. 정렬 없이
    자르면 마지막 고민의 근거가 통째로 날아가고 그 고민은 추천에서 조용히 빠진다.
    """
    captured: dict = {}

    async def _fake_gemini(prompt, context, candidates):
        captured["prompt"] = prompt
        return LlmOutput(picks=[LlmPick(name_kor="판테놀", reason="도움이 됩니다.")])

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)
    # 개별 청크는 _clip(1,500자)에 걸리므로, 총량 상한을 채우려면 여러 개가 필요하다.
    # 한 청크가 상한을 넘도록 만들면 clip 이 먼저 잘라 절단 자체가 일어나지 않는다.
    filler_count = MAX_EVIDENCE_CHARS // MAX_CHUNK_CHARS + 2
    doc_ids = [f"low_{i}" for i in range(filler_count)] + ["high"]
    candidates = [Candidate(name_kor="판테놀", score=0.9, source_doc_ids=doc_ids)]
    # 저score 를 앞자리에 몰아 둔다 — 정렬이 없으면 이것들이 상한을 먼저 다 먹는다
    chunks = [
        RetrievedChunk(
            content="관련 낮은 근거" + "가" * MAX_CHUNK_CHARS,
            score=0.5,
            source=ChunkSource(doc_id=f"low_{i}", title="t"),
            metadata={},
        )
        for i in range(filler_count)
    ]
    chunks.append(
        RetrievedChunk(
            content="관련 높은 근거",
            score=0.95,
            source=ChunkSource(doc_id="high", title="t"),
            metadata={},
        )
    )

    await generation.generate(_context(), candidates, chunks)

    assert "관련 높은 근거" in captured["prompt"]


def test_evidence_deduplicates_same_doc_id():
    """검색어가 겹치는 고민(진정 → 홍조·민감)에서 같은 행이 두 번 회수된다."""
    candidates = [Candidate(name_kor="판테놀", score=0.9, source_doc_ids=["eff_1"])]
    chunks = [_chunk_of("eff_1", "같은 근거"), _chunk_of("eff_1", "같은 근거")]

    block = generation._evidence_block(generation._relevant_chunks(chunks, candidates))

    assert block.count("같은 근거") == 1


async def test_regeneration_all_hallucinated_keeps_first_result(monkeypatch):
    """재생성이 전부 후보 밖을 고르면 1차를 되살린다.

    버리면 사용자는 근거가 있는데도 insufficient_evidence 를 받는다 — 1차는 금칙어만
    순화하면 쓸 수 있는 답이다.
    """
    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        if calls["n"] == 1:
            return LlmOutput(picks=[LlmPick(name_kor="판테놀", reason="여드름을 치료합니다.")])
        return LlmOutput(picks=[LlmPick(name_kor="존재하지않는성분", reason="정상 문장입니다.")])

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)

    picks = await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert calls["n"] == 2
    assert [p.name_kor for p in picks] == ["판테놀"]


async def test_generation_timeout_raises_502_not_hang(monkeypatch):
    """타임아웃이 없으면 Gemini 무응답 시 워커가 무기한 묶인다."""

    async def _hangs(prompt, context, candidates):
        await asyncio.sleep(3600)

    monkeypatch.setattr(generation, "_call_gemini", _hangs)
    monkeypatch.setattr(generation, "GENERATION_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(HTTPException) as exc:
        await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert exc.value.status_code == 502
