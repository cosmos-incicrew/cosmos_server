"""⑧ 제품 추천 테스트 — 커버리지 정렬·보유 제외·미매핑 스킵·장애 격리.

핵심 계약: 사용자에게 보인 추천 성분(narrative.recommended_names)과 일치하는 제품만,
커버리지(담은 추천 성분 수) 순으로, 보유 제품은 빼고 반환한다. 조회 실패는 예외가
아니라 빈 목록 — 제품은 부가 정보라 성분 추천을 깨뜨리면 안 된다.
"""

import pytest

from app.modules.recommendations.constants import MAX_RECOMMENDED_PRODUCTS
from app.modules.recommendations.pipeline import s8_products
from app.modules.recommendations.pipeline.s8_products import _rank, fetch
from app.modules.recommendations.schemas import Candidate, LlmNarrative
from tests.modules.recommendations.conftest import FakeSupabase


def _pi_row(ingredient_id: int, product_id: int, order_no: int, name: str = "제품") -> dict:
    """product_ingredients 한 행 (products 임베디드 조인 포함)."""
    return {
        "ingredient_id": ingredient_id,
        "order_no": order_no,
        "products": {
            "id": product_id,
            "product_name": f"{name}{product_id}",
            "brand": "브랜드",
            "product_url": f"http://x/{product_id}",
            "main_category": "스킨케어",
        },
    }


# ── _rank: 순수 정렬·집계 로직 ──────────────────────────────────


def test_coverage_ordering_puts_multi_match_product_first():
    id_to_name = {1: "나이아신아마이드", 2: "판테놀"}
    rows = [
        _pi_row(1, 100, order_no=5),  # 제품100: 성분1개
        _pi_row(1, 200, order_no=9),  # 제품200: 성분2개
        _pi_row(2, 200, order_no=3),
    ]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    assert [p.product_id for p in result] == [200, 100]
    assert result[0].matched_ingredients == ["나이아신아마이드", "판테놀"]  # 정렬됨
    assert result[1].matched_ingredients == ["나이아신아마이드"]


def test_min_order_no_breaks_tie():
    """매칭 수가 같으면 배합 상위(작은 order_no)가 앞선다."""
    id_to_name = {1: "성분A"}
    rows = [_pi_row(1, 100, order_no=8), _pi_row(1, 200, order_no=2)]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    assert [p.product_id for p in result] == [200, 100]


def test_owned_product_is_excluded():
    id_to_name = {1: "성분A"}
    rows = [_pi_row(1, 100, order_no=1), _pi_row(1, 200, order_no=1)]

    result = _rank(rows, id_to_name, owned_product_ids={100})

    assert [p.product_id for p in result] == [200]


def test_deleted_or_null_join_row_is_skipped():
    id_to_name = {1: "성분A"}
    rows = [{"ingredient_id": 1, "order_no": 1, "products": None}]

    assert _rank(rows, id_to_name, owned_product_ids=set()) == []


def test_caps_at_max_recommended_products():
    id_to_name = {1: "성분A"}
    rows = [_pi_row(1, pid, order_no=1) for pid in range(1, MAX_RECOMMENDED_PRODUCTS + 5)]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    assert len(result) == MAX_RECOMMENDED_PRODUCTS


# ── fetch: 성분 선별 + DB 조회 통합 ────────────────────────────


def _patch(monkeypatch: pytest.MonkeyPatch, tables=None, missing=None) -> None:
    async def _fake() -> FakeSupabase:
        return FakeSupabase(tables, missing)

    monkeypatch.setattr(s8_products, "get_supabase", _fake)


def _narrative(*names: str) -> LlmNarrative:
    return LlmNarrative(
        cause_analysis="원인", recommendation="추천", usage_guide="사용법",
        recommended_names=list(names),
    )


async def test_only_llm_recommended_and_mapped_ingredients_are_used(monkeypatch):
    """LLM 추천 목록에 있고(=사용자에게 보임) ingredient_id 가 매핑된 성분만 쓴다."""
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1),  # 추천+매핑 → 사용
        Candidate(name_kor="판테놀", score=0.8, ingredient_id=None),  # 미매핑 → 제외
        Candidate(name_kor="레티놀", score=0.7, ingredient_id=3),  # 추천목록에 없음 → 제외
    ]
    # product_ingredients 는 성분1(나이아신아마이드)·성분3(레티놀) 행을 다 담지만,
    # id_to_name 에 든 성분1만 매칭돼야 한다.
    _patch(monkeypatch, tables={"product_ingredients": [
        _pi_row(1, 100, order_no=1),
        _pi_row(3, 100, order_no=2),  # 레티놀 — 추천목록 밖이라 매칭 안 됨
    ]})

    result = await fetch(candidates, _narrative("나이아신아마이드", "판테놀"), owned_product_ids=[])

    assert len(result) == 1
    assert result[0].matched_ingredients == ["나이아신아마이드"]  # 레티놀 안 섞임


async def test_no_mapped_ingredient_skips_query(monkeypatch):
    """매핑된 추천 성분이 없으면 빈 목록 (쿼리 자체를 안 한다)."""
    candidates = [Candidate(name_kor="판테놀", score=0.8, ingredient_id=None)]
    # 테이블을 missing 으로 둬도, 쿼리를 스킵하므로 예외가 안 난다.
    _patch(monkeypatch, missing={"product_ingredients"})

    assert await fetch(candidates, _narrative("판테놀"), owned_product_ids=[]) == []


async def test_db_error_degrades_to_empty(monkeypatch):
    """조회 실패는 예외가 아니라 빈 목록 — 성분 추천은 그대로 나가야 한다."""
    candidates = [Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1)]
    _patch(monkeypatch, missing={"product_ingredients"})

    assert await fetch(candidates, _narrative("나이아신아마이드"), owned_product_ids=[]) == []
