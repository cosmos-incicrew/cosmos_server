"""DB 를 타는 안전·해소 로직 테스트 — 기존 스위트가 monkeypatch 로 잘라내던 경계.

s4 `resolve_ingredient_ids`(미매핑 40% 해소)와 s5 `fetch_restrictions`(사용제한 조회)는
파이프라인에서 가장 위험한 코드인데 그동안 목으로 대체돼 한 줄도 실행되지 않았다.
conftest 의 FakeSupabase 로 실제 조회 경로를 태운다.
"""


from app.modules.recommendations.pipeline import s4_candidates as candidates_stage
from app.modules.recommendations.pipeline import s5_safety as safety
from app.modules.recommendations.schemas import Candidate, UserContext
from tests.modules.recommendations.conftest import FakeSupabase


def _patch(module, monkeypatch, tables=None, missing=None) -> FakeSupabase:
    """가짜 클라이언트를 꽂고 그 인스턴스를 돌려준다 (`.calls` 로 조회 계약 단언)."""
    client = FakeSupabase(tables, missing)

    async def _fake() -> FakeSupabase:
        return client

    monkeypatch.setattr(module, "get_supabase", _fake)
    return client


def _context(**kwargs) -> UserContext:
    return UserContext(**{"user_id": "u1", "age": 32, "concerns": ["pores"], **kwargs})


# ── s4 resolve_ingredient_ids — 3단 조회(이름→이명→INCI) + INCI 개명 ──────


async def test_resolve_maps_via_three_tiers(monkeypatch):
    """정확 일치·이명·INCI 세 경로가 각각 ingredient_id 를 채운다."""
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9),  # ingredients 정확 일치
        Candidate(name_kor="글리세린별칭", score=0.8),  # synonyms 이명
        Candidate(name_kor="SULFUR", score=0.7),  # rec_efficacy.inci → 한글 개명
    ]
    _patch(candidates_stage, monkeypatch, tables={
        "ingredients": [
            {"ingredient_id": 1, "name_kor": "나이아신아마이드", "name_eng": "NIACINAMIDE"},
        ],
        "synonyms": [{"ingredient_id": 2, "synonym": "글리세린별칭"}],
        "rec_efficacy": [{"ingredient_id": 3, "inci": "SULFUR", "name_kor": "황"}],
    })

    await candidates_stage.resolve_ingredient_ids(candidates)

    assert candidates[0].ingredient_id == 1
    assert candidates[1].ingredient_id == 2
    # INCI 로 찾은 후보는 표시명이 한글로 개명된다 (영문 노출 방지).
    assert candidates[2].ingredient_id == 3
    assert candidates[2].name_kor == "황"
    assert candidates[2].inci == "SULFUR"


async def test_lookup_narrows_missing_names_across_the_three_tiers(monkeypatch):
    """조회 컬럼·순서·`missing` 계산이 계약이다.

    결과만 보면 세 경로를 아무 순서로 다 조회해도 통과한다. 실제 호출을 단언해
    (a) 컬럼명 오타(`name_kor`→`name_kr`), (b) tier 순서 뒤바뀜, (c) `missing` 재계산
    삭제(= 이미 찾은 이름까지 다음 tier 로 재조회) 를 모두 잡는다.
    """
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9),
        Candidate(name_kor="글리세린별칭", score=0.8),
        Candidate(name_kor="SULFUR", score=0.7),
    ]
    client = _patch(candidates_stage, monkeypatch, tables={
        "ingredients": [
            {"ingredient_id": 1, "name_kor": "나이아신아마이드", "name_eng": "NIACINAMIDE"},
        ],
        "synonyms": [{"ingredient_id": 2, "synonym": "글리세린별칭"}],
        "rec_efficacy": [{"ingredient_id": 3, "inci": "SULFUR", "name_kor": "황"}],
    })

    await candidates_stage.resolve_ingredient_ids(candidates)

    assert client.filters("ingredients") == [
        ("name_kor", ["나이아신아마이드", "글리세린별칭", "SULFUR"])
    ]
    # 1단에서 찾은 이름은 2단 조회에서 빠진다.
    assert client.filters("synonyms") == [("synonym", ["글리세린별칭", "SULFUR"])]
    assert client.filters("rec_efficacy") == [("inci", ["SULFUR"])]
    # 저렴한 경로부터 — 순서가 뒤집히면 이명·INCI 조회가 매번 전체 이름으로 나간다.
    assert [name for name, method, _ in client.calls if method == "in_"] == [
        "ingredients", "synonyms", "rec_efficacy"
    ]


async def test_lookup_selects_the_columns_each_tier_reads(monkeypatch):
    """SELECT 목록에서 컬럼이 빠지면 매칭은 되는데 값이 None 으로 채워진다."""
    client = _patch(candidates_stage, monkeypatch, tables={"ingredients": []})

    await candidates_stage.resolve_ingredient_ids([Candidate(name_kor="없는성분", score=0.9)])

    selects = {name: args[0] for name, method, args in client.calls if method == "select"}
    assert "name_kor" in selects["ingredients"] and "name_eng" in selects["ingredients"]
    assert "synonym" in selects["synonyms"]
    # rec_efficacy 는 INCI 로 찾고 한글 표시명으로 개명하므로 둘 다 필요하다.
    assert "inci" in selects["rec_efficacy"] and "name_kor" in selects["rec_efficacy"]


async def test_name_found_in_no_tier_stays_unmapped(monkeypatch):
    """세 경로 모두 못 찾은 후보는 미매핑으로 남는다 — ⑤의 명칭 보조 검사로 넘어간다.

    여기서 예외가 나거나 다른 행의 값을 잘못 집어 오면 엉뚱한 성분의 안전성 정보가
    붙는다.
    """
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9),
        Candidate(name_kor="어느표에도없는성분", score=0.8),
    ]
    _patch(candidates_stage, monkeypatch, tables={
        "ingredients": [
            {"ingredient_id": 1, "name_kor": "나이아신아마이드", "name_eng": "NIACINAMIDE"},
        ],
    })

    await candidates_stage.resolve_ingredient_ids(candidates)

    assert candidates[0].ingredient_id == 1
    assert candidates[1].ingredient_id is None
    assert candidates[1].name_kor == "어느표에도없는성분"  # 다른 행 이름으로 개명되지 않는다


async def test_resolve_swallows_db_error(monkeypatch):
    """조회 실패는 예외가 아니라 미매핑 유지 — 이후 ⑤ 명칭 보조 검사로 넘어간다."""
    candidates = [Candidate(name_kor="나이아신아마이드", score=0.9)]
    _patch(candidates_stage, monkeypatch, missing={"ingredients"})

    await candidates_stage.resolve_ingredient_ids(candidates)  # 예외 안 남

    assert candidates[0].ingredient_id is None


# ── s5 fetch_restrictions / apply_safety_filters ────────────────────────


async def test_banned_ingredient_is_removed(monkeypatch):
    _patch(safety, monkeypatch, tables={"restrictions": [
        {"ingredient_id": 1, "name_kor": "위험성분", "notice_ingr_name": None,
         "regulate_type": "금지", "limit_cond": None},
    ]})

    kept = await safety.apply_safety_filters(
        [Candidate(name_kor="위험성분", score=0.9, ingredient_id=1)], _context()
    )

    assert kept == []  # 금지는 후보에서 제거


async def test_unmapped_candidate_banned_only_by_notice_name_is_removed(monkeypatch):
    """고시 원문 명칭(`notice_ingr_name`)에만 등재된 금지 성분도 걸러야 한다.

    `ingredient_id` 가 없는 후보는 명칭 보조 검사가 유일한 방어선이다. 조회 컬럼을
    `name_kor` 하나로 줄이면 이 성분이 경고조차 없이 추천에 나간다.
    """
    _patch(safety, monkeypatch, tables={"restrictions": [
        {"ingredient_id": None, "name_kor": None, "notice_ingr_name": "고시명금지성분",
         "regulate_type": "금지", "limit_cond": None},
    ]})

    kept = await safety.apply_safety_filters(
        [Candidate(name_kor="고시명금지성분", score=0.9, ingredient_id=None)], _context()
    )

    assert kept == []


async def test_restriction_lookup_uses_both_name_columns(monkeypatch):
    """두 명칭 컬럼을 각각 in_() 으로 조회한다 — or_ 문자열 조립을 하지 않는다.

    성분명에 `,`·`"` 가 섞이면 조립한 필터가 깨져 조회가 **조용히 0건**이 되는데
    `lookup.ok` 는 True 라 금지 성분이 무경고로 통과한다.
    """
    client = _patch(safety, monkeypatch, tables={"restrictions": []})

    await safety.fetch_restrictions([Candidate(name_kor="어떤성분", score=0.9, ingredient_id=7)])

    assert client.filters("restrictions") == [
        ("ingredient_id", [7]),
        ("name_kor", ["어떤성분"]),
        ("notice_ingr_name", ["어떤성분"]),
    ]
    assert not any(method == "or_" for _, method, _ in client.calls)


async def test_limit_ingredient_gets_warning(monkeypatch):
    _patch(safety, monkeypatch, tables={"restrictions": [
        {"ingredient_id": 2, "name_kor": "한도성분", "notice_ingr_name": None,
         "regulate_type": "한도", "limit_cond": "세정 제품에 한해 3%"},
    ]})

    kept = await safety.apply_safety_filters(
        [Candidate(name_kor="한도성분", score=0.9, ingredient_id=2)], _context()
    )

    assert len(kept) == 1
    assert any(w.type == "한도" and "3%" in w.text for w in kept[0].warnings)


# ── s5 민감(S) 축 — 사용한도 + 자극 서술 성분 제외 (04 §12) ──────────────
#
# 실 DB 의 레조시놀 원문을 그대로 쓴다. 민감성 피부를 콕 집은 서술은 **영어 문장에만**
# 있다("especially in sensitive skin types").
_RESORCINOL_SAFETY_NOTE = (
    "다만, 과다 사용 시 피부 자극이나 알레르기 반응을 일으킬 수 있어 주의가 필요합니다.\n"
    "Considered safe at ≤2% in OTC acne products. Mild irritation or redness may occur, "
    "especially in sensitive skin types. Overuse should be avoided to prevent skin damage"
)
_RESORCINOL_RESTRICTION = {
    "ingredient_id": 3, "name_kor": "레조시놀", "notice_ingr_name": None,
    "regulate_type": "한도", "limit_cond": "기타 제품에 0.1%, 산화 염모에 2.0%",
}


def _resorcinol() -> Candidate:
    return Candidate(
        name_kor="레조시놀", score=0.9, ingredient_id=3,
        safety_note=_RESORCINOL_SAFETY_NOTE,
        recommended_skin_types="",  # 63% 가 빈값이라 이 컬럼으로는 못 거른다 (04 §1)
    )


async def test_irritant_limited_ingredient_removed_for_sensitive_type(monkeypatch):
    """DSPW(민감 S) 사용자에게 레조시놀이 대표 추천으로 나가던 회귀를 막는다."""
    _patch(safety, monkeypatch, tables={"restrictions": [_RESORCINOL_RESTRICTION]})

    kept = await safety.apply_safety_filters([_resorcinol()], _context(bsti_type="DSPW"))

    assert kept == []


async def test_irritant_limited_ingredient_kept_for_resistant_type(monkeypatch):
    """저항(R) 축은 기존 동작 그대로 — 제외하지 않고 한도 경고만 붙인다."""
    _patch(safety, monkeypatch, tables={"restrictions": [_RESORCINOL_RESTRICTION]})

    kept = await safety.apply_safety_filters([_resorcinol()], _context(bsti_type="DRPW"))

    assert len(kept) == 1
    assert any(w.type == "한도" for w in kept[0].warnings)


async def test_irritant_limited_ingredient_kept_without_bsti(monkeypatch):
    """BSTI 미검사(bsti_type=None) 사용자도 기존 동작 그대로다."""
    _patch(safety, monkeypatch, tables={"restrictions": [_RESORCINOL_RESTRICTION]})

    kept = await safety.apply_safety_filters([_resorcinol()], _context())

    assert len(kept) == 1
    assert any(w.type == "한도" for w in kept[0].warnings)


async def test_safe_limited_ingredient_kept_for_sensitive_type(monkeypatch):
    """한도 등재만으로 자르면 민감성 피부에 오히려 권장되는 성분까지 사라진다.

    징크옥사이드·티타늄디옥사이드·토코페롤이 실 DB 의 한도 45종에 들어 있다.
    """
    _patch(safety, monkeypatch, tables={"restrictions": [
        {"ingredient_id": 4, "name_kor": "징크옥사이드", "notice_ingr_name": None,
         "regulate_type": "한도", "limit_cond": "25%"},
    ]})

    kept = await safety.apply_safety_filters(
        [Candidate(
            name_kor="징크옥사이드", score=0.9, ingredient_id=4,
            safety_note="피부 자극이 적고, 광범위한 피부 타입에 적합합니다.\n"
                        "Highly safe, non-irritating. Rare mild irritation.",
        )],
        _context(bsti_type="DSPW"),
    )

    assert len(kept) == 1


async def test_english_only_irritation_note_is_read_for_sensitive_type(monkeypatch):
    """원문이 통째로 영어인 행이 실측 6건 있다 — 한국어만 훑으면 그 행을 통째로 놓친다."""
    _patch(safety, monkeypatch, tables={"restrictions": [
        {"ingredient_id": 6, "name_kor": "영어원문성분", "notice_ingr_name": None,
         "regulate_type": "한도", "limit_cond": "2%"},
    ]})

    kept = await safety.apply_safety_filters(
        [Candidate(
            name_kor="영어원문성분", score=0.9, ingredient_id=6,
            safety_note="Mild irritation may occur, especially in sensitive skin types.",
        )],
        _context(bsti_type="DSPW"),
    )

    assert kept == []


async def test_irritant_without_usage_limit_is_kept_for_sensitive_type(monkeypatch):
    """서술만으로는 자르지 않는다 — `자극` 단독 매칭은 성분 사전의 38% 에 걸린다."""
    _patch(safety, monkeypatch, tables={"restrictions": []})

    kept = await safety.apply_safety_filters(
        [Candidate(
            name_kor="무제한자극성분", score=0.9, ingredient_id=5,
            safety_note="과도한 사용 시 피부 자극을 유발할 수 있습니다.",
        )],
        _context(bsti_type="DSPW"),
    )

    assert len(kept) == 1


async def test_restriction_lookup_failure_is_fail_closed(monkeypatch):
    """조회 실패를 '제한 없음'으로 읽으면 안 된다 — 확인 불가 경고를 붙여 내보낸다."""
    _patch(safety, monkeypatch, missing={"restrictions"})

    kept = await safety.apply_safety_filters(
        [Candidate(name_kor="미매핑성분", score=0.9, ingredient_id=None)], _context()
    )

    assert len(kept) == 1
    assert any(w.type == "안전성확인불가" for w in kept[0].warnings)
