"""추천 파이프라인 단위 테스트 — ④ 집계 · ⑤ 안전성 필터 · ⑥ 환각 차단 · ⑩ 조립.

DB·Gemini 는 부르지 않는다. 순수 로직만 떼어 검증한다.
"""

import ast
import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.recommendations import bsti_traits
from app.modules.recommendations.constants import (
    BSTI_BOOST,
    CASE_INGREDIENT_BOOST,
    CASE_SKIN_TYPE_BOOST,
    MAX_CANDIDATES,
    MAX_CHUNK_CHARS,
    MAX_EVIDENCE_CHARS,
    MIN_CANDIDATES_PER_CONCERN,
    OWNED_PENALTY,
    PREGNANCY_AVOID,
)
from app.modules.recommendations.pipeline import s2_queries as queries_stage
from app.modules.recommendations.pipeline import s4_candidates as candidates_stage
from app.modules.recommendations.pipeline import s5_safety as safety
from app.modules.recommendations.pipeline import s6_generation as generation
from app.modules.recommendations.pipeline import s10_response as response_stage

# `_no_db` 픽스처가 모듈 속성을 목으로 갈아끼우므로, 실제 조회 경로를 태울 테스트를 위해
# 원본 함수를 임포트 시점에 붙잡아 둔다.
from app.modules.recommendations.pipeline.s4_candidates import (
    resolve_ingredient_ids as real_resolve_ingredient_ids,
)
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    RetrievedChunk,
    UserContext,
)
from app.modules.recommendations.util.ingredient_names import normalize_ingredient_name
from tests.modules.recommendations.conftest import FakeSupabase


def _efficacy_chunk(name: str, score: float, concern: str = "pores", **meta) -> RetrievedChunk:
    return RetrievedChunk(
        content=f"[성분] {name}",
        score=score,
        source=ChunkSource(doc_id=f"eff_{name}", title=f"{name} 효능"),
        metadata={"name_kor": name, "efficacy": f"{name} 효능", "concern": concern, **meta},
    )


def _case_chunk(
    names: list[str], score: float, concern: str = "pores", doc_id: str = "case_1", **meta
) -> RetrievedChunk:
    return RetrievedChunk(
        content="[상담] ...",
        score=score,
        source=ChunkSource(doc_id=doc_id, title="상담 사례"),
        metadata={"recommended_ingredients": names, "concern": concern, **meta},
    )


def _context(**kwargs) -> UserContext:
    base = {"user_id": "u1", "age": 32, "gender": "female", "concerns": ["pores"]}
    return UserContext(**{**base, **kwargs})


def test_generation_trace_disables_automatic_io_capture() -> None:
    """개인 컨텍스트는 decorator 자동 수집에서 차단하고 정제본만 수동 기록한다."""
    tree = ast.parse(inspect.getsource(generation._call_gemini))
    decorator = tree.body[0].decorator_list[0]

    assert isinstance(decorator, ast.Call)
    options = {keyword.arg: keyword.value for keyword in decorator.keywords}
    assert isinstance(options["capture_input"], ast.Constant)
    assert options["capture_input"].value is False
    assert isinstance(options["capture_output"], ast.Constant)
    assert options["capture_output"].value is False


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch):
    """④의 ingredient_id 조인·효능 채우기는 DB를 타므로 테스트에서는 건너뛴다.

    fill_missing_efficacy 를 no-op 으로 두면 케이스 유래 후보의 efficacy 가 안 채워져
    grounded 필터에서 빠진다 — DB 미매칭 시의 동작이다. 채워질 때 살아남는 경로는
    test_fill_missing_efficacy_rescues_case_ingredient 가 따로 검증한다.
    """

    async def _noop(candidates):
        return None

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _noop)
    monkeypatch.setattr(candidates_stage, "fill_missing_efficacy", _noop)


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


@pytest.mark.parametrize(
    ("code", "expected"),
    [("OSPW", "지성"), ("DRNT", "건성"), (None, None), ("XX", None)],
)
def test_bsti_maps_to_case_skin_type(code, expected):
    """상담 사례(`rec_cases.skin_type`)와 어휘가 달라 축 하나를 그 표기로 옮긴다."""
    assert bsti_traits.case_skin_type(code) == expected


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


async def test_weighted_score_does_not_leak_into_displayed_similarity():
    """가점은 정렬용이다 — ⑩ 카드의 similarity 는 코사인 원점수여야 한다.

    score 를 그대로 실으면 0.92 + BSTI_BOOST = 1.07 이 유사도로 나가고, 같은 성분이
    `ingredients[]`(청크 원점수 0.92)와 한 응답 안에서 두 값을 갖는다.
    """
    context = _context(bsti_recommended=["판테놀"])

    candidates = await candidates_stage.aggregate_candidates(
        [], [_efficacy_chunk("판테놀", 0.92)], context
    )
    cards = response_stage._top_ingredients([(candidates[0], "both")], context, {})

    assert candidates[0].score == pytest.approx(0.92 + BSTI_BOOST)  # 정렬에는 반영
    assert cards[0].similarity == 0.92


async def test_case_skin_type_match_boosts_candidate():
    """사용자 BSTI 유·수분 축과 피부타입이 같은 사례에서 온 성분을 위로 올린다.

    두 데이터의 피부타입 어휘가 달라 매핑(`bsti_traits.case_skin_type`)을 거친다.
    """
    candidates = await candidates_stage.aggregate_candidates(
        [_case_chunk(["판테놀"], 0.7, skin_type="지성")],
        [_efficacy_chunk("판테놀", 0.7)],
        _context(bsti_type="OSPW"),  # O = 지성
    )

    # 사례 유래 성분이라 CASE_INGREDIENT_BOOST 도 함께 붙는다 (협업 필터링 가점).
    assert candidates[0].score == pytest.approx(0.7 + CASE_SKIN_TYPE_BOOST + CASE_INGREDIENT_BOOST)


async def test_case_skin_type_boost_needs_a_bsti_type():
    """BSTI 미검사 사용자의 순위는 이 가점 도입 전과 같아야 한다."""
    candidates = await candidates_stage.aggregate_candidates(
        [_case_chunk(["판테놀"], 0.7, skin_type="지성")],
        [_efficacy_chunk("판테놀", 0.7)],
        _context(),  # bsti_type=None
    )

    # skin_type 가점(BSTI 축)은 안 붙지만 CASE_INGREDIENT_BOOST 는 사례 유래라 붙는다.
    assert candidates[0].score == pytest.approx(0.7 + CASE_INGREDIENT_BOOST)


async def test_unmapped_case_skin_type_is_kept_without_boost():
    """복합성·중성 사례는 가점만 없다 — 탈락시키면 안 된다.

    BSTI 1축은 O/D 2극뿐이라 실 데이터 8,000건의 69%(복합성 40%·중성 29%)가 어느 극에도
    대응하지 않는다. 하드필터로 쓰면 사례 3분의 2가 통째로 빠져 회수량이 무너진다.
    """
    # 사례 score(0.85)를 효능 근거(0.5)보다 높게, concern 도 다르게 둬 사례 기여가 산출에
    # 드러나게 한다 — 이름만 비교하면 효능 청크만으로도 통과해 탈락을 못 잡는다.
    cases = [
        _case_chunk(["판테놀"], 0.85, concern="wrinkles", skin_type="복합성", doc_id="c_mixed"),
        _case_chunk(["세라마이드"], 0.85, concern="wrinkles", skin_type="중성", doc_id="c_neutral"),
    ]

    candidates = await candidates_stage.aggregate_candidates(
        cases,
        [_efficacy_chunk("판테놀", 0.5), _efficacy_chunk("세라마이드", 0.5)],
        _context(bsti_type="OSPW"),
    )

    assert {c.name_kor for c in candidates} == {"판테놀", "세라마이드"}
    # skin_type 가점은 없지만(복합성·중성은 O/D 어느 극에도 대응 안 함) 사례 유래라
    # CASE_INGREDIENT_BOOST 는 붙는다 — 걸러졌다면 효능 청크의 0.5 에 머문다.
    assert all(c.score == pytest.approx(0.85 + CASE_INGREDIENT_BOOST) for c in candidates)
    assert all("wrinkles" in c.concerns for c in candidates)


async def test_owned_penalty_applied_once():
    chunks = [_efficacy_chunk("판테놀", 0.8), _efficacy_chunk("판테놀", 0.6)]

    candidates = await candidates_stage.aggregate_candidates(
        [], chunks, _context(owned_ingredients=["판테놀"])
    )

    assert candidates[0].score == pytest.approx(0.8 - OWNED_PENALTY)


async def test_case_ingredients_merge_into_candidates():
    """case 청크의 성분도 efficacy 근거가 있으면 후보에 합류하고, case 기여분(고민·score)도
    실제로 반영된다.

    efficacy 근거는 최소치(score 0.5, concern 기본값 "pores")만 대고, case 청크는 더 높은
    score(0.85)와 다른 concern("wrinkles")을 갖도록 해 — case-merge 루프(s4_candidates.py
    ``for chunk in case_chunks: ...``)가 통째로 죽어도 이 테스트가 실패하게 만든다. 이름
    집합만 비교하면 세라마이드가 efficacy 근거 하나만으로도 통과해 case 기여를 검증하지
    못한다.
    """
    candidates = await candidates_stage.aggregate_candidates(
        [_case_chunk(["세라마이드"], 0.85, concern="wrinkles")],
        [_efficacy_chunk("세라마이드", 0.5), _efficacy_chunk("판테놀", 0.8)],
        _context(),
    )

    assert {c.name_kor for c in candidates} == {"세라마이드", "판테놀"}
    ceramide = next(c for c in candidates if c.name_kor == "세라마이드")
    # case chunk 의 더 높은 score(0.85) + 사례 유래 가점(CASE_INGREDIENT_BOOST)
    assert ceramide.score == pytest.approx(0.85 + CASE_INGREDIENT_BOOST)
    assert "wrinkles" in ceramide.concerns  # case chunk 의 concern 도 반영됨


async def test_fill_missing_efficacy_rescues_case_ingredient(monkeypatch):
    """rec_efficacy 에 실재하는 case-only 성분은 효능이 채워져 grounded 를 통과한다.

    cases leg 정답 성분이 efficacy leg top-K 에 함께 뜨지 않아도 살아나는 경로 —
    held-out recall 을 0.11→0.36 으로 끌어올린 핵심 개조(fill_missing_efficacy)다.
    """

    async def _fill(candidates):  # rec_efficacy 매칭을 흉내 — 빈 효능을 채운다
        for candidate in candidates:
            if not candidate.efficacy:
                candidate.efficacy = "미백에 도움"

    monkeypatch.setattr(candidates_stage, "fill_missing_efficacy", _fill)

    candidates = await candidates_stage.aggregate_candidates(
        [_case_chunk(["알로에신"], 0.9, concern="brightening")],
        [],  # efficacy leg 는 이 성분을 회수하지 못했다
        _context(),
    )

    assert "알로에신" in {c.name_kor for c in candidates}


async def test_candidate_cap_enforced():
    chunks = [_efficacy_chunk(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_CANDIDATES + 5)]

    candidates = await candidates_stage.aggregate_candidates([], chunks, _context())

    assert len(candidates) == MAX_CANDIDATES


async def test_aggregate_drops_case_only_ingredients(monkeypatch):
    """efficacy 근거 없이 케이스에만 등장한 성분은 후보에서 제외된다 (설계 04 §3-1)."""
    from app.modules.recommendations.pipeline import s4_candidates
    from app.modules.recommendations.schemas import ChunkSource, RetrievedChunk, UserContext

    # efficacy 청크: 나이아신아마이드 (효능 근거 있음)
    eff = RetrievedChunk(
        content="", score=0.9, source=ChunkSource(doc_id="eff_1", title="t"),
        metadata={"name_kor": "나이아신아마이드", "efficacy": "미백", "ingredient_id": 1},
    )
    # 케이스 청크: 알로에신 (recommended_ingredients 로만 등장, efficacy 근거 없음)
    case = RetrievedChunk(
        content="", score=0.95, source=ChunkSource(doc_id="c1", title="t"),
        metadata={"recommended_ingredients": ["알로에신"], "concern": "brightening"},
    )
    ctx = UserContext(user_id="u1", age=30, concerns=["brightening"])

    async def _no_resolve(cands):  # ID 조회는 이 테스트 범위 밖
        return None
    monkeypatch.setattr(s4_candidates, "resolve_ingredient_ids", _no_resolve)

    result = await s4_candidates.aggregate_candidates([case], [eff], ctx)
    names = {c.name_kor for c in result}
    assert "나이아신아마이드" in names
    assert "알로에신" not in names  # case-only → 제외


# ── ④ 재현성 (같은 프로필 → 같은 추천) ─────────────────────────


async def test_tied_candidates_rank_identically_whatever_the_db_order():
    """동점 후보의 순서가 DB 행 도착 순서에 좌우되면 안 된다.

    검색 RPC 는 `order by embedding <=> query_embedding` 뿐이라(migration 015) 거리가
    같은 행의 순서가 보장되지 않는다. score 만으로 정렬하면 파이썬 안정 정렬이 그 순서를
    그대로 물려받아, 상한(MAX_CANDIDATES)에서 잘리는 성분이 실행마다 달라진다 — 같은
    프로필로 두 번 돌린 데모에서 추천 3개 중 2개가 바뀐 원인이다.
    """
    names = [f"성분{i}" for i in range(MAX_CANDIDATES + 3)]
    arrival_orders = [names, list(reversed(names)), names[5:] + names[:5]]

    outcomes = set()
    for order in arrival_orders:
        chunks = [_efficacy_chunk(name, 0.71) for name in order]  # 전부 동점
        result = await candidates_stage.aggregate_candidates([], chunks, _context())
        outcomes.add(tuple(c.name_kor for c in result))

    assert len(outcomes) == 1, f"도착 순서마다 다른 후보가 나온다: {outcomes}"


async def test_duplicate_lookup_key_resolves_to_a_fixed_row(monkeypatch):
    """한 INCI 에 rec_efficacy 행이 여럿일 때 이기는 행이 고정돼야 한다.

    `.in_()` 조회에는 ORDER BY 가 없어 먼저 온 행이 이기면 같은 요청에도 성분 ID 와
    한글 표시명이 실행마다 바뀐다 — 표시명이 바뀌면 ⑧ 대표 선정까지 흔들린다.
    """
    duplicates = [
        {"ingredient_id": 9, "inci": "SULFUR", "name_kor": "황(고농도)"},
        {"ingredient_id": 3, "inci": "SULFUR", "name_kor": "황"},
    ]

    resolved = []
    for order in (duplicates, list(reversed(duplicates))):
        client = FakeSupabase({"ingredients": [], "synonyms": [], "rec_efficacy": order})
        monkeypatch.setattr(candidates_stage, "get_supabase", _returning(client))
        candidate = Candidate(name_kor="SULFUR", score=0.7)

        await real_resolve_ingredient_ids([candidate])

        resolved.append((candidate.ingredient_id, candidate.name_kor))

    assert resolved[0] == resolved[1] == (3, "황")


def _returning(client: FakeSupabase):
    async def _fake() -> FakeSupabase:
        return client

    return _fake


# ── ④ 고민별 최소 슬롯 (설계 04 §2-1b) ─────────────────────────


async def test_weak_concern_keeps_minimum_candidate_slots():
    """검색 점수가 높은 고민이 낮은 고민을 후보 목록 밖으로 밀어내면 안 된다.

    ⑥은 후보 목록 안에서만 추천하므로, 밀려난 고민은 응답에서 통째로 사라진다.
    """
    strong = [
        _efficacy_chunk(f"미백{i}", 0.9 - i * 0.001, concern="brightening")
        for i in range(MAX_CANDIDATES + 5)
    ]
    weak = [_efficacy_chunk(f"진정{i}", 0.6 - i * 0.001, concern="redness") for i in range(3)]

    candidates = await candidates_stage.aggregate_candidates(
        [], strong + weak, _context(concerns=["brightening", "redness"])
    )

    assert len(candidates) == MAX_CANDIDATES
    # 예약분도 그 고민 안에서는 점수순이다
    assert [c.name_kor for c in candidates if "redness" in c.concerns] == [
        f"진정{i}" for i in range(MIN_CANDIDATES_PER_CONCERN)
    ]


async def test_concern_without_evidence_reserves_no_slot():
    """근거 없는 고민까지 자리를 예약하면 근거 있는 고민의 성분만 줄어든다.

    그 상태는 ⑩ advisory 의 partial_evidence 가 따로 알린다.
    """
    chunks = [
        _efficacy_chunk(f"성분{i}", 0.9 - i * 0.01, concern="brightening")
        for i in range(MAX_CANDIDATES + 3)
    ]

    candidates = await candidates_stage.aggregate_candidates(
        [], chunks, _context(concerns=["brightening", "redness", "acne"])
    )

    assert [c.name_kor for c in candidates] == [f"성분{i}" for i in range(MAX_CANDIDATES)]


async def test_single_concern_user_keeps_pure_score_order():
    """고민이 하나면 예약이 상위 몇 개를 다시 집는 것뿐이라 도입 전과 결과가 같다."""
    chunks = [_efficacy_chunk(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_CANDIDATES + 3)]

    candidates = await candidates_stage.aggregate_candidates([], chunks, _context())

    assert [c.name_kor for c in candidates] == [f"성분{i}" for i in range(MAX_CANDIDATES)]


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


async def test_pregnancy_contraindicated_removed_when_nursing(_no_restrictions):
    """수유 중도 제외 대상이다.

    `expecting` 판정에서 `is_nursing` 을 빼면 수유부에게 레티놀이 **경고조차 없이**
    추천된다 — expecting 도 unknown_pregnancy 도 False 가 되기 때문이다.
    """
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(
        candidates, _context(is_pregnant=False, is_nursing=True)
    )

    assert kept == []


async def test_contraindicated_ingredient_not_warned_when_neither_pregnant_nor_nursing(
    _no_restrictions,
):
    """임신·수유가 아님이 확정된 사용자에겐 제외 대상 성분도 경고 없이 나간다.

    `unknown_pregnancy` 게이트를 없애면 모든 사용자에게 임신 경고가 붙는다.
    """
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(
        candidates, _context(is_pregnant=False, is_nursing=False)
    )

    assert len(kept) == 1
    assert not any(w.type == "임신수유주의" for w in kept[0].warnings)


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


def test_banned_claim_sentence_removed():
    text = "여드름을 치료해 줍니다. 피부 진정에 도움이 됩니다."

    assert generation.sanitize_claims(text) == "피부 진정에 도움이 됩니다."


def test_all_sentences_banned_falls_back_to_safe_text():
    assert "근거" in generation.sanitize_claims("질환을 완치합니다.")


# ── ⑩ 응답 조립 ───────────────────────────────────────────────


def test_insufficient_suggests_bsti_when_not_taken():
    no_bsti = response_stage.insufficient(_context(), "no_evidence", "없음")
    assert no_bsti.advisory.action == "take_bsti"
    with_bsti = response_stage.insufficient(_context(bsti_type="OSPW"), "no_evidence", "없음")
    assert with_bsti.advisory.action == "retry_with_other_concerns"


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


def test_build_queries_leads_with_concern():
    """질의는 고민을 문두에 두어 사람묘사(BSTI) 편향을 줄인다 (설계 04 §2-1)."""
    ctx = _context(age=30, gender="female", bsti_type="DSPW", concerns=["redness"])
    queries = queries_stage.build_queries(ctx)
    code, q = queries[0]
    assert code == "redness"
    # 고민 라벨(붉어짐...)이 사람묘사(건성·민감...)보다 앞에 온다
    assert q.index("붉어짐") < q.index("건성")


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
#
# 금지 제거·조회 실패 fail-closed 는 실 조회 경로로 test_db_logic 이 검증한다.
# 여기 목 기반 중복본은 같은 계약을 약하게 반복해 삭제했다.


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
    # efficacy 근거 필수 필터를 통과시키기 위한 근거 (매핑 전 한글명 키에 붙는다).
    efficacy = [_efficacy_chunk("헥사펩타이드-2", 0.5)]

    candidates = await candidates_stage.aggregate_candidates(chunks, efficacy, _context())

    assert len(candidates) == 1
    assert candidates[0].name_kor == "헥사펩타이드-2"
    assert set(candidates[0].concerns) == {"wrinkles", "pores"}


async def test_collapsed_candidates_union_concerns_and_source_docs(monkeypatch):
    """매핑 후 합쳐지는 두 후보의 고민·근거는 **합집합**이어야 한다.

    두 축의 값이 겹치지 않게 구성한다 — 겹쳐 두면 병합 코드를 통째로 지워도 통과한다.
    잃어버린 doc_id 는 ⑥ 프롬프트 근거 선별(`_relevant_chunks`)에서 그 근거를 통째로
    탈락시킨다.
    """

    async def _resolve(candidates):
        for c in candidates:
            if c.name_kor == "Hexapeptide-2":
                c.ingredient_id = 4871
                c.name_kor = "헥사펩타이드-2"
            elif c.name_kor == "헥사펩타이드-2":
                c.ingredient_id = 4871

    monkeypatch.setattr(candidates_stage, "resolve_ingredient_ids", _resolve)
    chunks = [
        _case_chunk(["헥사펩타이드-2"], 0.9, concern="wrinkles", doc_id="case_ko"),
        _case_chunk(["Hexapeptide-2"], 0.85, concern="pores", doc_id="case_inci"),
    ]
    # efficacy 근거도 한글 후보 쪽 고민(wrinkles)에만 달아 INCI 후보의 기여를 분리한다.
    efficacy = [_efficacy_chunk("헥사펩타이드-2", 0.5, concern="wrinkles")]

    candidates = await candidates_stage.aggregate_candidates(chunks, efficacy, _context())

    assert len(candidates) == 1
    assert set(candidates[0].concerns) == {"wrinkles", "pores"}
    assert set(candidates[0].source_doc_ids) == {
        "case_ko", "case_inci", "eff_헥사펩타이드-2"
    }


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

    response = response_stage.insufficient(context, "no_evidence", "근거 없음")

    assert response.status == "insufficient_evidence"
    assert response.advisory.code == "no_evidence"
    assert response.advisory.action == "take_bsti"  # BSTI 미검사 → 검사 유도
    assert response.cases == [] and response.top_ingredients == []
    assert response.user_profile.concerns == ["sensitivity"]


# ── ⑥ 재생성·에러 트레이스 ─────────────────────────────────────


async def test_banned_claim_triggers_one_regeneration(monkeypatch):
    from app.modules.recommendations.pipeline import s6_generation as generation
    from app.modules.recommendations.schemas import Candidate, LlmNarrative

    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        rec = "여드름을 치료합니다." if calls["n"] == 1 else "피부 진정에 도움이 됩니다."
        return LlmNarrative(
            cause_analysis="원인", recommendation=rec, usage_guide="사용법",
            recommended_names=["판테놀"],
        )

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)
    result = await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])
    assert calls["n"] == 2
    assert "치료" not in result.recommendation


async def test_hallucinated_name_triggers_regeneration(monkeypatch):
    from app.modules.recommendations.pipeline import s6_generation as generation
    from app.modules.recommendations.schemas import Candidate, LlmNarrative

    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        names = ["존재하지않는성분"] if calls["n"] == 1 else ["판테놀"]
        return LlmNarrative(
            cause_analysis="원인", recommendation="판테놀 추천", usage_guide="사용법",
            recommended_names=names,
        )

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)
    await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])
    assert calls["n"] == 2


async def test_regeneration_prompt_carries_the_failure_reason(monkeypatch):
    """재시도 프롬프트에 실패 사유가 실려야 한다 (설계 04 §9).

    온도 0·고정 seed 라 같은 프롬프트를 다시 보내면 같은 답이 온다 — 사유를 싣지 않으면
    재생성이 같은 값에 LLM 호출만 한 번 더 쓰고 오염된 결과를 그대로 쓴다.
    """
    from app.modules.recommendations.schemas import LlmNarrative

    prompts: list[str] = []

    async def _fake_gemini(prompt, context, candidates):
        prompts.append(prompt)
        names = ["존재하지않는성분"] if len(prompts) == 1 else ["판테놀"]
        return LlmNarrative(
            cause_analysis="원인", recommendation="추천", usage_guide="사용법",
            recommended_names=names,
        )

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)

    await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert len(prompts) == 2
    assert "다시 작성" not in prompts[0]  # 첫 시도엔 붙지 않는다 (빈 섹션이 지시를 희석시킨다)
    assert prompts[0] in prompts[1]  # 후보·근거는 그대로 두고 뒤에 덧붙인다
    # 이름까지 실려야 모델이 무엇을 빼야 하는지 안다
    assert "존재하지않는성분" in prompts[1]


def test_recommend_count_is_exact_with_bsti_and_a_range_without():
    """BSTI 있으면 하한==상한(3)이라 '정확히 3', 없으면 '3~5' 범위로 렌더한다."""
    from app.modules.recommendations.constants import (
        MAX_RECOMMENDED,
        MAX_RECOMMENDED_WITH_BSTI,
        MIN_RECOMMENDED,
    )

    with_bsti = generation._recommend_count(MAX_RECOMMENDED_WITH_BSTI)
    assert with_bsti == f"정확히 {MAX_RECOMMENDED_WITH_BSTI}"
    assert generation._recommend_count(MAX_RECOMMENDED) == f"{MIN_RECOMMENDED}~{MAX_RECOMMENDED}"


async def test_too_few_recommended_names_triggers_regeneration(monkeypatch):
    """빈 목록(또는 MIN_RECOMMENDED 미만)이면 재생성한다.

    그대로 통과시키면 ⑧·⑨이 쓸 성분이 없어 메인 카드·제품이 통째로 빈 채 status=ok 로
    나간다 — 서사 본문에는 성분 설명이 그대로 남아 있어 더 어긋난다.
    """
    from app.modules.recommendations.schemas import LlmNarrative

    names = ["판테놀", "세라마이드", "아데노신"]
    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        return LlmNarrative(
            cause_analysis="원인", recommendation="추천", usage_guide="사용법",
            recommended_names=[] if calls["n"] == 1 else names,
        )

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)
    candidates = [Candidate(name_kor=n, score=0.9) for n in names]

    result = await generation.generate(_context(), candidates, [])

    assert calls["n"] == 2
    assert result.recommended_names == names


async def test_min_names_capped_by_candidate_count(monkeypatch):
    """후보가 MIN_RECOMMENDED 보다 적으면 그 수까지만 요구한다 (못 채울 수를 요구하면
    매번 재생성만 하고 끝난다)."""
    from app.modules.recommendations.schemas import LlmNarrative

    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        return LlmNarrative(
            cause_analysis="원인", recommendation="추천", usage_guide="사용법",
            recommended_names=["판테놀"],
        )

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


def test_candidate_block_labels_the_concern_each_candidate_answers():
    """고민 코드가 없으면 "고민마다 최소 하나"(템플릿 요청)를 모델이 판단할 수 없다.

    정렬은 재현성 몫이다 — `concerns` 는 검색 결과 도착 순서로 쌓여 그대로 쓰면 같은
    후보로도 프롬프트 문자열이 실행마다 달라진다.
    """
    candidates = [Candidate(name_kor="성분A", score=0.9, concerns=["redness", "brightening"])]

    assert generation._candidate_block(candidates) == "- 성분A [brightening, redness]"


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

    ⑩의 순화로 처리 가능한 문제인데 502 로 버리면, 일시적 오류가 멀쩡한 추천을 죽인다.
    """
    from app.modules.recommendations.schemas import LlmNarrative

    calls = {"n": 0}

    async def _fake_gemini(prompt, context, candidates):
        calls["n"] += 1
        if calls["n"] == 1:
            return LlmNarrative(
                cause_analysis="원인", recommendation="여드름을 치료합니다.",
                usage_guide="사용법", recommended_names=["판테놀"],
            )
        raise ConnectionError("일시적 오류")

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)

    result = await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert calls["n"] == 2
    assert result.recommended_names == ["판테놀"], "1차 결과를 502 로 버리면 안 된다"
    assert "치료" not in generation.sanitize_claims(result.recommendation)


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
    from app.modules.recommendations.schemas import LlmNarrative

    captured: dict = {}

    async def _fake_gemini(prompt, context, candidates):
        captured["prompt"] = prompt
        return LlmNarrative(
            cause_analysis="원인", recommendation="판테놀 추천", usage_guide="사용법",
            recommended_names=["판테놀"],
        )

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


def test_langfuse_output_omits_echoed_personal_attributes():
    ctx = _context(
        user_id="user-123",
        age=32,
        gender="female",
        owned_ingredients=["판테놀"],
        owned_products_by_ingredient={"판테놀": ["보습크림"]},
        is_pregnant=True,
    )
    output = "user-123 32세 여성 임신 사용자는 보습크림의 판테놀을 보유"

    redacted = generation._redact_model_output(output, ctx)

    for personal_value in ["user-123", "32", "여성", "임신", "보습크림", "판테놀"]:
        assert personal_value not in redacted


async def test_gemini_call_records_only_redacted_output(monkeypatch):
    """helper가 아니라 실제 Gemini→Langfuse 기록 경로가 정제본을 쓰는지 검증한다."""
    ctx = _context(
        user_id="user-123",
        age=32,
        gender="female",
        owned_ingredients=["판테놀"],
        owned_products_by_ingredient={"판테놀": ["보습크림"]},
        is_pregnant=True,
    )
    raw = json.dumps(
        {
            "cause_analysis": "user-123 32세 여성 임신",
            "recommendation": "보습크림의 판테놀",
            "usage_guide": "사용법",
            "recommended_names": ["판테놀"],
        },
        ensure_ascii=False,
    )
    trace_updates: list[dict[str, object]] = []

    async def _generate_content(**kwargs):
        del kwargs
        return SimpleNamespace(text=raw)

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    )
    monkeypatch.setattr(generation, "get_gemini", lambda: client)
    monkeypatch.setattr(generation, "_safe_trace", lambda **fields: trace_updates.append(fields))

    call = getattr(generation._call_gemini, "__wrapped__", generation._call_gemini)
    result = await call("프롬프트", ctx, [])

    traced_output = next(update["output"] for update in trace_updates if "output" in update)
    assert isinstance(traced_output, str)
    for personal_value in ["user-123", "32", "여성", "임신", "보습크림", "판테놀"]:
        assert personal_value not in traced_output
    assert result.recommendation == "보습크림의 판테놀"


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


async def test_pregnancy_unknown_warns_with_text_that_says_not_advised(_no_restrictions):
    """미수집(unknown) 상태는 제거가 아니라 경고다 — 일괄 제거는 과차단이다.

    문구도 '금기'가 아니라 '권고되지 않음'이어야 한다 (근거보다 세게 쓰면 안 된다).
    """
    candidates = [Candidate(name_kor="레티놀", score=0.9, ingredient_id=1)]

    kept = await safety.apply_safety_filters(candidates, _context())

    assert len(kept) == 1
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
    ⑩의 보유 배지는 개명 후 이름으로 판정한다 — "보유 표시는 되는데 하향은 안 된" 후보.
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
    from app.modules.recommendations.schemas import LlmNarrative

    captured: dict = {}

    async def _fake_gemini(prompt, context, candidates):
        captured["prompt"] = prompt
        return LlmNarrative(
            cause_analysis="원인", recommendation="판테놀 추천", usage_guide="사용법",
            recommended_names=["판테놀"],
        )

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


async def test_generation_timeout_raises_502_not_hang(monkeypatch):
    """타임아웃이 없으면 Gemini 무응답 시 워커가 무기한 묶인다."""

    async def _hangs(prompt, context, candidates):
        await asyncio.sleep(3600)

    monkeypatch.setattr(generation, "_call_gemini", _hangs)
    monkeypatch.setattr(generation, "GENERATION_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(HTTPException) as exc:
        await generation.generate(_context(), [Candidate(name_kor="판테놀", score=0.9)], [])

    assert exc.value.status_code == 502


async def test_assemble_builds_narrative_response():
    from app.modules.recommendations.pipeline import s8_top_picks, s10_response
    from app.modules.recommendations.schemas import (
        Candidate,
        ChunkSource,
        IngredientWarning,
        LlmNarrative,
        RetrievedChunk,
    )

    case = RetrievedChunk(
        content="…", score=0.8,
        source=ChunkSource(doc_id="c1", title="37 홍조 상담"),
        metadata={"target_concern": "홍조", "skin_type": "건성", "gender": "여성",
                  "age": 37, "recommended_ingredients": ["판테놀", "쑥잎추출물"],
                  "question": "질문", "answer": "답변", "concern": "pores"},
    )
    eff = RetrievedChunk(
        content="…", score=0.7,
        source=ChunkSource(doc_id="eff_1", title="판테놀 효능"),
        metadata={"name_kor": "판테놀", "inci": "PANTHENOL", "efficacy": "진정",
                  "safety_note": "고농도 주의", "recommended_concentration": "1~5%",
                  "concern": "pores"},
    )
    cand = Candidate(
        name_kor="판테놀", score=0.9, inci="PANTHENOL",
        safety_note="고농도 주의", recommended_concentration="1~5%",
        warnings=[
            IngredientWarning(type="알레르기유발", text="첩포 검사 권장"),
            IngredientWarning(type="주의사항", text="ⓧ 이건 safety_note 로 대체되어 제외"),
        ],
    )
    narrative = LlmNarrative(
        cause_analysis="원인 분석", recommendation="판테놀 추천", usage_guide="사용법",
        recommended_names=["판테놀"],
    )

    resp = s10_response.assemble(
        _context(), narrative, [case], [eff],
        [(cand, s8_top_picks.SOURCE_CONCERN)], [],
    )
    assert resp.status == "ok"
    assert resp.answer.cause_analysis == "원인 분석"
    assert resp.answer.recommendation == "판테놀 추천"
    assert resp.cases[0].target_concern == "홍조"
    assert resp.cases[0].skin_type == "건성"
    assert resp.cases[0].recommended_ingredients == ["판테놀", "쑥잎추출물"]
    ing = resp.top_ingredients[0]
    assert ing.name_kor == "판테놀"
    assert ing.safety_note == "고농도 주의" and ing.concentration == "1~5%"
    assert [w.type for w in ing.warnings] == ["알레르기유발"]  # 주의사항 제외
    assert resp.user_profile.concerns  # UserProfile 로 이름 변경됨
    # 경고는 성분별로 귀속한다 — 응답 최상단의 flat 배열은 없어야 한다.
    assert "warnings" not in resp.model_dump()
    assert resp.advisory is None  # 케이스 근거 있으니 알림 없음


def test_partial_evidence_advisory_names_uncovered_concern():
    """일부 고민만 근거가 있으면 partial_evidence 로 누락 고민을 알린다 (설계 04 §3-2)."""
    from app.modules.recommendations.pipeline.s10_response import assemble
    from app.modules.recommendations.schemas import (
        ChunkSource,
        LlmNarrative,
        RetrievedChunk,
        UserContext,
    )

    ctx = UserContext(user_id="u1", age=30, concerns=["redness", "brightening"])
    narrative = LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=["나이아신아마이드"],
    )
    # brightening 고민만 근거가 있고 redness 는 0건
    eff = RetrievedChunk(
        content="", score=0.9, source=ChunkSource(doc_id="eff_1", title="t"),
        metadata={"name_kor": "나이아신아마이드", "efficacy": "미백", "concern": "brightening"},
    )
    case = RetrievedChunk(
        content="", score=0.8, source=ChunkSource(doc_id="c1", title="t"),
        metadata={"target_concern": "미백", "recommended_ingredients": ["나이아신아마이드"],
                  "concern": "brightening"},
    )
    resp = assemble(ctx, narrative, [case], [eff], [], [])
    assert resp.advisory is not None
    assert resp.advisory.code == "partial_evidence"
    assert "붉어짐" in resp.advisory.message  # CONCERN_LABEL_BY_CODE["redness"]


def test_case_recommended_ingredients_strip_newlines_and_dedupe():
    """케이스 근거의 성분명은 개행·괄호를 떼고 중복을 제거해 노출한다.

    정규화는 표기 정리까지만 한다 — 한글명과 INCI(`알로에신`/`ALOESIN`)를 합치는 것은
    ④의 ID 매핑 몫이라 여기서는 별개 항목으로 남는다.
    """
    from app.modules.recommendations.pipeline.s10_response import _case
    from app.modules.recommendations.schemas import ChunkSource, RetrievedChunk

    chunk = RetrievedChunk(
        content="", score=0.8, source=ChunkSource(doc_id="c1", title="t"),
        metadata={
            "target_concern": "미백",
            "recommended_ingredients": ["알로에신", "ALOESIN", "알로에신\n", "레티놀(비타민 A)"],
        },
    )
    ev = _case(chunk)

    assert ev.recommended_ingredients == ["알로에신", "ALOESIN", "레티놀"]


def test_weak_evidence_advisory_when_cases_leg_died_but_efficacy_survived():
    """고민 근거는 다 있는데 상담 사례가 통째로 없으면 weak_evidence 다.

    `partial_evidence` 가 먼저 걸리는 상황과 구분한다 — 이 조합(cases leg 만 실패)은
    ③ `_retrieve_one` 의 leg 단위 부분 실패에서 실제로 나오는 상태다.
    """
    from app.modules.recommendations.pipeline.s10_response import (
        CODE_WEAK_EVIDENCE,
        WEAK_EVIDENCE_MESSAGE,
        assemble,
    )
    from app.modules.recommendations.schemas import LlmNarrative

    ctx = _context(concerns=["pores"])
    narrative = LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=["판테놀"],
    )
    eff = _efficacy_chunk("판테놀", 0.9, concern="pores")

    resp = assemble(ctx, narrative, [], [eff], [], [])

    assert resp.status == "ok"  # 근거가 약할 뿐 추천은 한다
    assert resp.advisory is not None
    assert resp.advisory.code == CODE_WEAK_EVIDENCE
    assert resp.advisory.message == WEAK_EVIDENCE_MESSAGE


def test_partial_evidence_wins_over_weak_evidence():
    """근거 없는 고민이 있으면 사례가 통째로 없어도 partial_evidence 로 그 고민을 지목한다."""
    from app.modules.recommendations.pipeline.s10_response import CODE_PARTIAL_EVIDENCE, assemble
    from app.modules.recommendations.schemas import LlmNarrative

    ctx = _context(concerns=["pores", "redness"])
    narrative = LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=["판테놀"],
    )
    eff = _efficacy_chunk("판테놀", 0.9, concern="pores")

    resp = assemble(ctx, narrative, [], [eff], [], [])

    assert resp.advisory.code == CODE_PARTIAL_EVIDENCE
    assert "붉어짐" in resp.advisory.message


def test_assemble_routes_top_lists_to_their_own_slots():
    """⑩ 은 ⑧ 대표 성분과 그 제품을 각자 자리에 싣는다 (인자 뒤바뀜 방지)."""
    from app.modules.recommendations.pipeline.s8_top_picks import SOURCE_BSTI
    from app.modules.recommendations.pipeline.s10_response import assemble
    from app.modules.recommendations.schemas import (
        Candidate,
        LlmNarrative,
        ProductRecommendation,
    )

    ctx = _context()
    narrative = LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=["판테놀"],
    )
    bsti_cand = Candidate(name_kor="세라마이드", score=1.0)

    resp = assemble(
        ctx,
        narrative,
        [],
        [_efficacy_chunk("판테놀", 0.9)],
        [(bsti_cand, SOURCE_BSTI)],
        [ProductRecommendation(
            product_id=3, product_name="제품3", matched_ingredients=["세라마이드"]
        )],
    )

    assert [(i.name_kor, i.match_source) for i in resp.top_ingredients] == [
        ("세라마이드", SOURCE_BSTI)
    ]
    assert [p.product_id for p in resp.top_products] == [3]


async def test_assemble_empty_answer_returns_insufficient():
    from app.modules.recommendations.pipeline import s10_response
    from app.modules.recommendations.schemas import LlmNarrative

    resp = s10_response.assemble(
        _context(),
        LlmNarrative(cause_analysis="  ", recommendation="  ", usage_guide="  "),
        [], [], [], [],
    )
    assert resp.status == "insufficient_evidence"
    assert resp.answer is None


# ── ⑥ 영어 원문 번역 요청 ───────────────────────────────────────
#
# `rec_efficacy` 원본에 통째로 영어인 행이 있다(제품 연결 가능분 6건). 지우면 설명 칸이
# 비므로 ⑥ 생성에 번역을 얹는다 — 그 요청이 프롬프트에 제대로/필요할 때만 실리는지.


async def _capture_prompt(monkeypatch, candidates, bsti_candidates=None) -> str:
    from app.modules.recommendations.schemas import LlmNarrative

    captured: dict = {}

    async def _fake_gemini(prompt, context, cands):
        captured["prompt"] = prompt
        return LlmNarrative(
            cause_analysis="원인", recommendation="추천", usage_guide="사용법",
            recommended_names=[c.name_kor for c in candidates],
        )

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)
    await generation.generate(_context(), candidates, [], bsti_candidates)
    return captured["prompt"]


async def test_no_translation_block_when_nothing_is_english(monkeypatch):
    """대부분의 요청이 이 경우다 — 빈 섹션을 늘 붙이면 지시가 희석되고 토큰만 든다."""
    candidates = [
        Candidate(
            name_kor="판테놀",
            score=0.9,
            efficacy="보습 효과\n피부 진정",
            safety_note="대부분 피부 타입에 안전합니다.",
        )
    ]

    prompt = await _capture_prompt(monkeypatch, candidates)

    assert "영어 원문 번역" not in prompt


async def test_translation_block_covers_bsti_axis_too(monkeypatch):
    """BSTI 축 성분의 영어도 번역 대상이다 — ⑦을 ⑥ 앞으로 옮긴 이유가 이것이다.

    병풀추출물은 `BSTI_RECOMMENDED` 16타입 중 8개(민감 S 계열 전부)에 들어 있어 자주
    노출된다. ⑦이 ⑥과 병렬이던 시절엔 프롬프트를 만들 때 BSTI 후보가 아직 없었다.
    """
    concern = [
        Candidate(
            name_kor="콜라겐",
            score=0.9,
            efficacy="Improves skin elasticity and hydration.",
            safety_note="피부 자극이 적습니다.",
        )
    ]
    bsti = [
        Candidate(
            name_kor="병풀추출물",
            score=1.0,
            efficacy="피부 진정에 도움을 줍니다.",
            safety_note="Generally recognized as safe for sensitive skin.",
        )
    ]

    prompt = await _capture_prompt(monkeypatch, concern, bsti)

    assert "영어 원문 번역" in prompt
    assert "Improves skin elasticity" in prompt
    assert "Generally recognized as safe" in prompt
    # 한국어가 있는 칸은 요청하지 않는다 — 번역이 아니라 재작성이 된다.
    assert "피부 자극이 적습니다" not in prompt.split("## 영어 원문 번역")[1]
    assert "피부 진정에 도움을 줍니다" not in prompt.split("## 영어 원문 번역")[1]


def test_translation_block_asks_once_for_an_ingredient_in_both_axes():
    """두 축에 겹친 성분의 같은 원문을 두 번 번역시키지 않는다."""
    shared = Candidate(name_kor="콜라겐", score=0.9, efficacy="Improves skin elasticity.")

    block = generation._translation_block([shared, shared])

    assert block.count("- 콜라겐") == 1
