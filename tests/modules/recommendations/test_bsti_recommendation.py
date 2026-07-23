"""⑦ BSTI 추천 테스트 — 타입 권장 성분 조회·근거 카드 조립·가지 격리.

핵심 계약: BSTI 는 검색이 아니라 확정 표라 표 순서를 지키고, 효능 사전 근거가 있는
성분만 싣는다. 부가 정보이므로 어떤 실패도 고민 추천을 깨뜨리지 않는다.
"""

from app.modules.recommendations import service
from app.modules.recommendations.pipeline import (
    s5_safety,
    s7_bsti,
    s9_products,
    s10_response,
)
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


async def test_db_error_degrades_to_empty(monkeypatch):
    """조회 실패는 예외가 아니라 빈 목록 — 고민 추천은 그대로 나가야 한다."""
    _patch_db(monkeypatch, missing={"rec_efficacy"})

    context = UserContext(user_id="u", bsti_recommended=["세라마이드"])

    assert await s7_bsti.fetch_bsti_candidates(context) == []


# ── s7: BSTI 근거 카드 ──────────────────────────────────────────


def test_bsti_card_uses_similarity_one_and_drops_duplicate_note():
    """표 확정 매칭이라 similarity=1.0.

    `주의사항` 경고는 safety_note 와 중복이라 뺀다 — `_ingredient` 와 같은 규칙이다.
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

    cards = s10_response._bsti_ingredients([candidate], context)

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

    assert await service._bsti_branch(UserContext(user_id="u")) == ([], [])


async def test_branch_skips_safety_and_products_when_no_candidates(monkeypatch):
    """후보가 없으면 ⑤·⑨을 부르지 않는다 (불필요한 DB 왕복 차단)."""
    called: list[str] = []

    async def _none(context):
        return []

    async def _safety(candidates, context):
        called.append("s5")
        return candidates

    async def _products(candidates, names, owned):
        called.append("s8")
        return []

    monkeypatch.setattr(s7_bsti, "fetch_bsti_candidates", _none)
    monkeypatch.setattr(s5_safety, "apply_safety_filters", _safety)
    monkeypatch.setattr(s9_products, "fetch", _products)

    assert await service._bsti_branch(UserContext(user_id="u")) == ([], [])
    assert called == []


async def test_branch_sends_only_safe_names_to_products(monkeypatch):
    """제품 조회는 ⑤ 통과분 이름만 받는다 — 걸러진 성분(임신 금기 등)의 제품이 섞이면 안 된다."""
    captured: dict = {}

    async def _candidates(context):
        return [
            Candidate(name_kor="세라마이드", score=1.0),
            Candidate(name_kor="레티놀", score=1.0),
        ]

    async def _safety(candidates, context):
        return [c for c in candidates if c.name_kor != "레티놀"]  # 임신 금기 제거 흉내

    async def _products(candidates, names, owned):
        captured["names"] = names
        return []

    monkeypatch.setattr(s7_bsti, "fetch_bsti_candidates", _candidates)
    monkeypatch.setattr(s5_safety, "apply_safety_filters", _safety)
    monkeypatch.setattr(s9_products, "fetch", _products)

    safe, _ = await service._bsti_branch(UserContext(user_id="u"))

    assert [c.name_kor for c in safe] == ["세라마이드"]
    assert captured["names"] == {"세라마이드"}
