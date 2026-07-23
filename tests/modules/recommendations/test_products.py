"""⑨ 제품 추천 테스트 — 성분 커버리지 그리디·보유 제외·미매핑 스킵·장애 격리.

핵심 계약: 사용자에게 보인 추천 성분(narrative.recommended_names)과 일치하는 제품만,
추천 성분을 골고루 커버하도록 그리디로 고른다 — 다중 매칭 제품 우선, 아직 안 나온
성분을 담은 제품 우선, 이미 커버된 성분만 담은 제품은 제외(커버 완료면 멈춤). 보유
제품은 빼고, 조회 실패는 예외가 아니라 빈 목록.
"""

import pytest

from app.modules.recommendations.constants import MAX_RECOMMENDED_PRODUCTS
from app.modules.recommendations.pipeline import s9_products
from app.modules.recommendations.pipeline.s9_products import _rank, fetch
from app.modules.recommendations.schemas import Candidate
from tests.modules.recommendations.conftest import FakeSupabase


def _pi_row(
    ingredient_id: int, product_id: int, order_no: int, name: str = "제품", brand: str = "브랜드"
) -> dict:
    """product_ingredients 한 행 (products 임베디드 조인 포함)."""
    return {
        "ingredient_id": ingredient_id,
        "order_no": order_no,
        "products": {
            "id": product_id,
            # 원본은 크롤링 그대로라 프로모션 문구가 붙어 있고, 표시는 정제본을 쓴다.
            "product_name": f"[프로모션] {name}{product_id} 기획(+증정)",
            "cleaned_product_name": f"{name}{product_id}",
            "brand": brand,
            "product_url": f"http://x/{product_id}",
            "main_category": "스킨케어",
        },
    }


# ── _rank: 성분 커버리지 그리디 ──────────────────────────────────


def test_multi_match_product_comes_first_then_new_coverage():
    """다중 매칭 제품이 먼저, 그다음 아직 안 나온 성분을 담은 제품이 온다.

    이미 커버된 성분만 담은 제품(중복)은 제외된다.
    """
    id_to_name = {1: "나이아신아마이드", 2: "판테놀", 3: "세라마이드"}
    rows = [
        _pi_row(1, 100, order_no=5),  # 제품100: 나이아 1개 (200과 중복 → 제외될 것)
        _pi_row(1, 200, order_no=9),  # 제품200: 나이아·판테놀 2개
        _pi_row(2, 200, order_no=3),
        _pi_row(3, 300, order_no=1),  # 제품300: 세라마이드 1개 (새 성분)
    ]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    # 200(2커버) → 300(미커버 세라마이드) → 100은 나이아 중복이라 제외
    assert [p.product_id for p in result] == [200, 300]
    assert result[0].matched_ingredients == ["나이아신아마이드", "판테놀"]


def test_stops_when_no_new_coverage():
    """이미 커버된 성분만 담은 제품은 노출하지 않는다 (커버 완료면 멈춤)."""
    id_to_name = {1: "성분A"}
    rows = [_pi_row(1, pid, order_no=pid) for pid in (100, 200, 300)]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    # 성분A 하나뿐 — 한 제품이 커버하면 나머지는 새로 더할 게 없어 제외. 1개만.
    assert len(result) == 1


def test_min_order_no_breaks_tie_on_first_pick():
    """첫 선택에서 매칭이 동률이면 배합 상위(작은 order_no)가 뽑힌다."""
    id_to_name = {1: "성분A"}
    rows = [_pi_row(1, 100, order_no=8), _pi_row(1, 200, order_no=2)]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    assert [p.product_id for p in result] == [200]


def test_owned_product_is_excluded():
    id_to_name = {1: "성분A"}
    rows = [_pi_row(1, 100, order_no=1), _pi_row(1, 200, order_no=2)]

    result = _rank(rows, id_to_name, owned_product_ids={100})

    assert [p.product_id for p in result] == [200]


def test_deleted_or_null_join_row_is_skipped():
    id_to_name = {1: "성분A"}
    rows = [{"ingredient_id": 1, "order_no": 1, "products": None}]

    assert _rank(rows, id_to_name, owned_product_ids=set()) == []


def test_caps_at_max_recommended_products():
    """추천 성분이 상한보다 많으면 최대 MAX_RECOMMENDED_PRODUCTS 개까지만 (전부 새 커버여도)."""
    n = MAX_RECOMMENDED_PRODUCTS + 3
    id_to_name = {i: f"성분{i}" for i in range(1, n + 1)}
    # 각기 다른 성분을 하나씩 담은 서로 다른 제품 — 모두 새 커버지만 상한에서 절단
    rows = [_pi_row(i, 100 + i, order_no=1) for i in range(1, n + 1)]

    result = _rank(rows, id_to_name, owned_product_ids=set())

    assert len(result) == MAX_RECOMMENDED_PRODUCTS


def test_display_name_uses_cleaned_column():
    """표시명은 정제 컬럼을 쓴다 — 크롤링 원본의 프로모션 문구를 노출하지 않는다."""
    rows = [_pi_row(1, 100, order_no=1)]

    result = _rank(rows, {1: "성분A"}, owned_product_ids=set())

    assert result[0].product_name == "제품100"


def test_display_name_falls_back_to_raw_when_cleaned_missing():
    """정제 컬럼이 비면 원본으로 폴백한다 — 이름 없는 제품이 나가면 안 된다."""
    row = _pi_row(1, 100, order_no=1)
    row["products"]["cleaned_product_name"] = None

    result = _rank([row], {1: "성분A"}, owned_product_ids=set())

    assert result[0].product_name == "[프로모션] 제품100 기획(+증정)"


# ── fetch: 성분 선별 + DB 조회 통합 ────────────────────────────


def _patch(monkeypatch: pytest.MonkeyPatch, tables=None, missing=None) -> None:
    async def _fake() -> FakeSupabase:
        return FakeSupabase(tables, missing)

    monkeypatch.setattr(s9_products, "get_supabase", _fake)


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

    result = await fetch(candidates, {"나이아신아마이드", "판테놀"}, owned_product_ids=[])

    assert len(result) == 1
    assert result[0].matched_ingredients == ["나이아신아마이드"]  # 레티놀 안 섞임


async def test_no_mapped_ingredient_skips_query(monkeypatch):
    """매핑된 추천 성분이 없으면 빈 목록 (쿼리 자체를 안 한다)."""
    candidates = [Candidate(name_kor="판테놀", score=0.8, ingredient_id=None)]
    # 테이블을 missing 으로 둬도, 쿼리를 스킵하므로 예외가 안 난다.
    _patch(monkeypatch, missing={"product_ingredients"})

    assert await fetch(candidates, {"판테놀"}, owned_product_ids=[]) == []


async def test_db_error_degrades_to_empty(monkeypatch):
    """조회 실패는 예외가 아니라 빈 목록 — 성분 추천은 그대로 나가야 한다."""
    candidates = [Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1)]
    _patch(monkeypatch, missing={"product_ingredients"})

    assert await fetch(candidates, {"나이아신아마이드"}, owned_product_ids=[]) == []
