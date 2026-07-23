"""DB 를 타는 안전·해소 로직 테스트 — 기존 스위트가 monkeypatch 로 잘라내던 경계.

s4 `resolve_ingredient_ids`(미매핑 40% 해소)와 s5 `fetch_restrictions`(사용제한 조회)는
파이프라인에서 가장 위험한 코드인데 그동안 목으로 대체돼 한 줄도 실행되지 않았다.
conftest 의 FakeSupabase 로 실제 조회 경로를 태운다.
"""


from app.modules.recommendations.pipeline import s4_candidates as candidates_stage
from app.modules.recommendations.pipeline import s5_safety as safety
from app.modules.recommendations.schemas import Candidate, UserContext
from tests.modules.recommendations.conftest import FakeSupabase


def _patch(module, monkeypatch, tables=None, missing=None) -> None:
    async def _fake() -> FakeSupabase:
        return FakeSupabase(tables, missing)

    monkeypatch.setattr(module, "get_supabase", _fake)


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


async def test_restriction_lookup_failure_is_fail_closed(monkeypatch):
    """조회 실패를 '제한 없음'으로 읽으면 안 된다 — 확인 불가 경고를 붙여 내보낸다."""
    _patch(safety, monkeypatch, missing={"restrictions"})

    kept = await safety.apply_safety_filters(
        [Candidate(name_kor="미매핑성분", score=0.9, ingredient_id=None)], _context()
    )

    assert len(kept) == 1
    assert any(w.type == "안전성확인불가" for w in kept[0].warnings)
