"""⑦ BSTI 추천 테스트 — 타입 권장 성분 조회·근거 카드 조립·가지 격리.

핵심 계약: BSTI 는 검색이 아니라 확정 표라 표 순서를 지키고, 효능 사전 근거가 있는
성분만 싣는다. 부가 정보이므로 어떤 실패도 고민 추천을 깨뜨리지 않는다.
"""

from app.modules.recommendations import bsti_ingredients, service
from app.modules.recommendations.constants import EFFICACY_FIELDS
from app.modules.recommendations.pipeline import s5_safety, s7_bsti, s10_response
from app.modules.recommendations.schemas import Candidate, IngredientWarning, UserContext
from tests.modules.recommendations.conftest import FakeSupabase


def _eff_row(name: str, **over) -> dict:
    """rec_efficacy 한 행."""
    row = {
        "ingredient_id": 1,
        "inci": f"{name}-INCI",
        "name_kor": name,
        "efficacy": f"{name} 효능",
        "safety_note": None,
        "recommended_concentration": "1~5%",
        "recommended_skin_types": "건성",
        "regulation_note": None,
    }
    row.update(over)
    return row


def _patch_db(monkeypatch, tables=None, missing=None) -> None:
    async def _fake() -> FakeSupabase:
        return FakeSupabase(tables, missing)

    monkeypatch.setattr(s7_bsti, "get_supabase", _fake)


# ── s4b: BSTI 후보 조회 ─────────────────────────────────────────


async def test_no_bsti_type_returns_empty_without_query(monkeypatch):
    """검사 전(권장 성분 없음)이면 DB 를 치지 않는다 — 테이블이 없어도 예외가 안 난다."""
    _patch_db(monkeypatch, missing={"rec_efficacy"})

    context = UserContext(user_id="u", bsti_recommended=[])

    assert await s7_bsti.fetch_bsti_candidates(context) == []


async def test_keeps_table_order_and_fills_efficacy(monkeypatch):
    """표 순서를 그대로 유지하고 효능·농도 근거를 채운다 (DB 반환 순서로 뒤집히지 않는다)."""
    _patch_db(
        monkeypatch,
        tables={
            "rec_efficacy": [
                _eff_row("판테놀", ingredient_id=2),  # DB 는 표와 다른 순서로 준다
                _eff_row("세라마이드", ingredient_id=1),
            ]
        },
    )

    context = UserContext(user_id="u", bsti_recommended=["세라마이드", "판테놀"])
    result = await s7_bsti.fetch_bsti_candidates(context)

    assert [c.name_kor for c in result] == ["세라마이드", "판테놀"]
    assert result[0].ingredient_id == 1
    assert result[0].efficacy == "세라마이드 효능"
    assert result[0].recommended_concentration == "1~5%"
    assert result[0].score == 1.0  # 검색 유사도가 아니라 확정 매칭


async def test_name_missing_from_efficacy_dictionary_is_skipped(monkeypatch):
    """효능 사전에 없는 표 이름은 싣지 않는다 — 근거 없이 추천하지 않는다."""
    _patch_db(monkeypatch, tables={"rec_efficacy": [_eff_row("세라마이드")]})

    context = UserContext(user_id="u", bsti_recommended=["세라마이드", "없는성분"])
    result = await s7_bsti.fetch_bsti_candidates(context)

    assert [c.name_kor for c in result] == ["세라마이드"]


async def test_unmapped_ingredient_id_is_excluded(monkeypatch):
    """식약처 미연결(ingredient_id=null) 성분은 후보에서 뺀다.

    ⑨ 제품 조회가 ingredient_id 로 역조인하므로 제품이 구조적으로 0건이고, ⑤가
    `안전성확인불가` 경고를 붙인다. 그런데 ⑧이 BSTI 에 대표 카드 자리를 조건 없이
    내주므로, 걸러내지 않으면 "제품도 없고 안전성도 모르는 성분"이 메인 카드에 오른다.
    """
    _patch_db(
        monkeypatch,
        tables={
            "rec_efficacy": [
                _eff_row("세라마이드", ingredient_id=None),
                _eff_row("판테놀", ingredient_id=2),
            ]
        },
    )

    context = UserContext(user_id="u", bsti_recommended=["세라마이드", "판테놀"])
    result = await s7_bsti.fetch_bsti_candidates(context)

    assert [c.name_kor for c in result] == ["판테놀"]


async def test_all_unmapped_yields_empty_instead_of_falling_back(monkeypatch):
    """전부 미연결이면 빈 목록 — 미연결 성분을 되살리는 폴백은 두지 않는다.

    폴백을 두면 제외한 이유(제품 0건 · 안전성 미확인)가 그대로 돌아온다. 실측상
    16개 타입 모두 제외 후 6개 이상이 남아 이 경로는 실데이터에서 발생하지 않는다.
    """
    _patch_db(
        monkeypatch,
        tables={"rec_efficacy": [_eff_row("세라마이드", ingredient_id=None)]},
    )

    context = UserContext(user_id="u", bsti_recommended=["세라마이드"])

    assert await s7_bsti.fetch_bsti_candidates(context) == []


async def test_same_name_twice_in_the_table_yields_one_candidate(monkeypatch):
    """한 타입 안에서 같은 이름이 두 번 나와도 후보는 하나다.

    `_DB_ALIASES` 가 1:N 확장이라(`비타민C 유도체`·`아연`) 같은 DB 표기가 두 번 들어올
    수 있다. 그대로 두면 BSTI 카드에 같은 성분이 두 번 나오고 제품 커버 목표도 부풀려진다.
    """
    _patch_db(monkeypatch, tables={"rec_efficacy": [_eff_row("세라마이드")]})

    context = UserContext(user_id="u", bsti_recommended=["세라마이드", "세라마이드"])
    result = await s7_bsti.fetch_bsti_candidates(context)

    assert [c.name_kor for c in result] == ["세라마이드"]


async def test_efficacy_columns_cover_every_field_the_candidate_carries(monkeypatch):
    """SELECT 목록이 EFFICACY_FIELDS 를 전부 덮어야 한다.

    빠진 컬럼은 예외가 아니라 None 으로 조용히 채워져 BSTI 카드에서만 효능·농도가 빈다.
    """
    client = FakeSupabase({"rec_efficacy": [_eff_row("세라마이드")]})

    async def _fake() -> FakeSupabase:
        return client

    monkeypatch.setattr(s7_bsti, "get_supabase", _fake)

    await s7_bsti.fetch_bsti_candidates(
        UserContext(user_id="u", bsti_recommended=["세라마이드"])
    )

    columns = next(args[0] for name, method, args in client.calls if method == "select")
    for field in (*EFFICACY_FIELDS, "name_kor"):
        assert field in columns, f"{field} 컬럼이 SELECT 에서 빠졌다"
    assert client.filters("rec_efficacy") == [("name_kor", ["세라마이드"])]


async def test_db_error_degrades_to_empty(monkeypatch):
    """조회 실패는 예외가 아니라 빈 목록 — 고민 추천은 그대로 나가야 한다."""
    _patch_db(monkeypatch, missing={"rec_efficacy"})

    context = UserContext(user_id="u", bsti_recommended=["세라마이드"])

    assert await s7_bsti.fetch_bsti_candidates(context) == []


# ── s7: BSTI 근거 카드 ──────────────────────────────────────────


def test_candidate_card_drops_duplicate_note():
    """`주의사항` 경고는 safety_note 와 중복이라 뺀다.

    BSTI 축 카드의 similarity 는 ⑩이 null 로 지운다(표 매칭이라 검색 유사도가 아니다).
    그건 match_source 를 넘기는 경로의 계약이라 test_response_quality 가 맡는다.
    """
    candidate = Candidate(
        name_kor="세라마이드",
        score=1.0,
        inci="CERAMIDE",
        efficacy="보습",
        safety_note="원본 주의",
        recommended_concentration="1%",
        warnings=[
            IngredientWarning(type="주의사항", text="중복이라 빠짐"),
            IngredientWarning(type="임신수유주의", text="남아야 함"),
        ],
    )
    context = UserContext(
        user_id="u",
        owned_ingredients=["세라마이드"],
        owned_products_by_ingredient={"세라마이드": ["보유크림"]},
    )

    cards = [s10_response._evidence_from_candidate(candidate, context)]

    assert cards[0].similarity == 1.0
    assert cards[0].safety_note == "원본 주의"
    assert [w.type for w in cards[0].warnings] == ["임신수유주의"]
    assert cards[0].owned is True
    assert cards[0].owned_products == ["보유크림"]


# ── service: BSTI 가지 격리 ─────────────────────────────────────


async def test_branch_failure_is_isolated(monkeypatch):
    """가지가 터져도 빈 결과일 뿐 — 고민 추천은 그대로 나간다."""

    async def _boom(context):
        raise RuntimeError("bsti down")

    monkeypatch.setattr(s7_bsti, "fetch_bsti_candidates", _boom)

    assert await service._bsti_branch(UserContext(user_id="u")) == []


async def test_branch_skips_safety_when_no_candidates(monkeypatch):
    """후보가 없으면 ⑤를 부르지 않는다 (불필요한 DB 왕복 차단)."""
    called: list[str] = []

    async def _none(context):
        return []

    async def _safety(candidates, context):
        called.append("s5")
        return candidates

    monkeypatch.setattr(s7_bsti, "fetch_bsti_candidates", _none)
    monkeypatch.setattr(s5_safety, "apply_safety_filters", _safety)

    assert await service._bsti_branch(UserContext(user_id="u")) == []
    assert called == []


async def test_branch_returns_only_safe_candidates(monkeypatch):
    """가지 산출은 ⑤ 통과분뿐이다 — 걸러진 성분(임신 금기 등)이 ⑧ 종합으로 새면 안 된다."""

    async def _candidates(context):
        return [
            Candidate(name_kor="세라마이드", score=1.0),
            Candidate(name_kor="레티놀", score=1.0),
        ]

    async def _safety(candidates, context):
        return [c for c in candidates if c.name_kor != "레티놀"]  # 임신 금기 제거 흉내

    monkeypatch.setattr(s7_bsti, "fetch_bsti_candidates", _candidates)
    monkeypatch.setattr(s5_safety, "apply_safety_filters", _safety)

    safe = await service._bsti_branch(UserContext(user_id="u"))

    assert [c.name_kor for c in safe] == ["세라마이드"]


def test_alias_family_takes_one_slot_not_three():
    """`히알루론산` 한 항목의 별칭 3개가 대표 카드를 전부 먹던 회귀."""
    groups = bsti_ingredients.group_by_table_entry(
        [
            "하이알루로닉애씨드",
            "소듐하이알루로네이트",
            "하이드롤라이즈드하이알루로닉애씨드",
            "판테놀",
        ]
    )

    assert [len(g) for g in groups] == [3, 1]
