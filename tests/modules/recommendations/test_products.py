"""⑨ 제품 추천 테스트 — 성분 커버리지 그리디·보유 제외·미매핑 스킵·장애 격리.

핵심 계약: 사용자에게 보인 추천 성분(narrative.recommended_names)과 일치하는 제품만,
추천 성분을 골고루 커버하도록 그리디로 고른다 — 다중 매칭 제품 우선, 아직 안 나온
성분을 담은 제품 우선, 이미 커버된 성분만 담은 제품은 제외(커버 완료면 멈춤). 보유
제품은 빼고, 조회 실패는 예외가 아니라 빈 목록.
"""

import pytest

from app.modules.recommendations.constants import (
    MAX_RECOMMENDED_PRODUCTS,
    PRODUCT_FETCH_LIMIT,
)
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

    이미 커버된 성분만 담은 제품은 커버가 한 바퀴 돈 뒤에야 뒤로 붙는다.
    """
    id_to_names = {1: {"나이아신아마이드"}, 2: {"판테놀"}, 3: {"세라마이드"}}
    rows = [
        _pi_row(1, 100, order_no=5),  # 제품100: 나이아 1개 (200과 중복 → 뒤로)
        _pi_row(1, 200, order_no=9),  # 제품200: 나이아·판테놀 2개
        _pi_row(2, 200, order_no=3),
        _pi_row(3, 300, order_no=1),  # 제품300: 세라마이드 1개 (새 성분)
    ]

    result = _rank(rows, id_to_names, owned_product_ids=set())

    # 200(2커버) → 300(미커버 세라마이드) → 한 바퀴 끝, 남은 100
    assert [p.product_id for p in result] == [200, 300, 100]
    assert result[0].matched_ingredients == ["나이아신아마이드", "판테놀"]


def test_fills_up_to_cap_when_one_ingredient_covers_everything():
    """커버를 다 해도 상한까지 채운다 — 제품 1개만 내보내지 않는다.

    커버 완료에서 멈추면 대표 성분 중 제품 있는 성분이 적을 때 살 수 있는 제품이 한두
    개로 쪼그라든다 (설계 04 §4-2 실측).
    """
    id_to_names = {1: {"성분A"}}
    rows = [_pi_row(1, 100 + i, order_no=i) for i in range(MAX_RECOMMENDED_PRODUCTS + 2)]

    result = _rank(rows, id_to_names, owned_product_ids=set())

    assert len(result) == MAX_RECOMMENDED_PRODUCTS


def test_products_without_any_recommended_ingredient_stay_out():
    """상한까지 채우더라도 추천 성분을 하나도 안 담은 제품은 들어오지 않는다."""
    id_to_names = {1: {"성분A"}}
    rows = [_pi_row(1, 100, order_no=1), _pi_row(9, 900, order_no=1)]  # 성분9는 추천 밖

    result = _rank(rows, id_to_names, owned_product_ids=set())

    assert [p.product_id for p in result] == [100]


def test_min_order_no_breaks_tie_on_first_pick():
    """첫 선택에서 매칭이 동률이면 배합 상위(작은 order_no)가 뽑힌다."""
    id_to_names = {1: {"성분A"}}
    rows = [_pi_row(1, 100, order_no=8), _pi_row(1, 200, order_no=2)]

    result = _rank(rows, id_to_names, owned_product_ids=set())

    assert [p.product_id for p in result] == [200, 100]


def test_owned_product_is_excluded():
    id_to_names = {1: {"성분A"}}
    rows = [_pi_row(1, 100, order_no=1), _pi_row(1, 200, order_no=2)]

    result = _rank(rows, id_to_names, owned_product_ids={100})

    assert [p.product_id for p in result] == [200]


def test_deleted_or_null_join_row_is_skipped():
    id_to_names = {1: {"성분A"}}
    rows = [{"ingredient_id": 1, "order_no": 1, "products": None}]

    assert _rank(rows, id_to_names, owned_product_ids=set()) == []


def test_caps_at_max_recommended_products():
    """추천 성분이 상한보다 많으면 최대 MAX_RECOMMENDED_PRODUCTS 개까지만 (전부 새 커버여도)."""
    n = MAX_RECOMMENDED_PRODUCTS + 3
    id_to_names = {i: {f"성분{i}"} for i in range(1, n + 1)}
    # 각기 다른 성분을 하나씩 담은 서로 다른 제품 — 모두 새 커버지만 상한에서 절단
    rows = [_pi_row(i, 100 + i, order_no=1) for i in range(1, n + 1)]

    result = _rank(rows, id_to_names, owned_product_ids=set())

    assert len(result) == MAX_RECOMMENDED_PRODUCTS


def test_ingredients_without_products_do_not_shrink_the_product_list():
    """제품 0건인 대표 성분이 섞여도 제품 수가 줄지 않는다 (설계 04 §4-2 실측 재현).

    실제로 나온 모양 그대로다 — 대표 성분 5개 중 둘(에필로비움 로세움추출물·레조시놀)은
    `product_ingredients` 에 행이 없고, 남은 셋 중 둘은 같은 제품에 함께 들어 있다.
    커버 완료에서 멈추면 제품이 2개로 끝났다.
    """
    id_to_names = {
        4871: {"헥사펩타이드-2"},
        4822: {"하이알루로닉애씨드"},
        1265: {"소듐하이알루로네이트"},
        # 16569 에필로비움·5487 레조시놀은 행이 없어 아예 나타나지 않는다
    }
    rows = []
    for pid in range(200, 210):  # 히알루론산 계열 두 성분을 함께 담은 제품들
        rows += [_pi_row(4822, pid, order_no=3), _pi_row(1265, pid, order_no=4)]
    rows += [_pi_row(4871, pid, order_no=5) for pid in (300, 301)]  # 헥사펩타이드 제품

    result = _rank(rows, id_to_names, owned_product_ids=set())

    assert len(result) == MAX_RECOMMENDED_PRODUCTS
    # 성분 편중 방지: 제품이 2개뿐인 헥사펩타이드도 뒤 슬롯에서 자리를 잡는다.
    hexa = [p for p in result if "헥사펩타이드-2" in p.matched_ingredients]
    assert len(hexa) == 2


def test_display_name_uses_cleaned_column():
    """표시명은 정제 컬럼을 쓴다 — 크롤링 원본의 프로모션 문구를 노출하지 않는다."""
    rows = [_pi_row(1, 100, order_no=1)]

    result = _rank(rows, {1: {"성분A"}}, owned_product_ids=set())

    assert result[0].product_name == "제품100"


def test_display_name_falls_back_to_raw_when_cleaned_missing():
    """정제 컬럼이 비면 원본으로 폴백한다 — 이름 없는 제품이 나가면 안 된다."""
    row = _pi_row(1, 100, order_no=1)
    row["products"]["cleaned_product_name"] = None

    result = _rank([row], {1: {"성분A"}}, owned_product_ids=set())

    assert result[0].product_name == "[프로모션] 제품100 기획(+증정)"


# ── fetch: 성분 선별 + DB 조회 통합 ────────────────────────────


def _patch(monkeypatch: pytest.MonkeyPatch, tables=None, missing=None) -> FakeSupabase:
    client = FakeSupabase(tables, missing)

    async def _fake() -> FakeSupabase:
        return client

    monkeypatch.setattr(s9_products, "get_supabase", _fake)
    return client


async def test_only_llm_recommended_and_mapped_ingredients_are_used(monkeypatch):
    """LLM 추천 목록에 있고(=사용자에게 보임) ingredient_id 가 매핑된 성분만 쓴다."""
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1),  # 추천+매핑 → 사용
        Candidate(name_kor="판테놀", score=0.8, ingredient_id=None),  # 미매핑 → 제외
        Candidate(name_kor="레티놀", score=0.7, ingredient_id=3),  # 추천목록에 없음 → 제외
    ]
    # product_ingredients 는 성분1(나이아신아마이드)·성분3(레티놀) 행을 다 담지만,
    # id_to_names 에 든 성분1만 매칭돼야 한다.
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


async def test_same_id_with_two_display_names_keeps_both(monkeypatch):
    """같은 성분을 두 축이 다른 이름으로 부르면 이름이 하나 사라지면 안 된다.

    id→이름을 1:1 로 담으면 뒤 항목이 앞을 덮어, 커버리지 그리디가 커버 목표를 실제보다
    적게 잡는다.
    """
    candidates = [
        Candidate(name_kor="히알루론산", score=0.9, ingredient_id=77),
        Candidate(name_kor="하이알루로닉애씨드", score=1.0, ingredient_id=77),
    ]
    _patch(monkeypatch, tables={"product_ingredients": [_pi_row(77, 100, order_no=1)]})

    result = await fetch(
        candidates, {"히알루론산", "하이알루로닉애씨드"}, owned_product_ids=[]
    )

    assert result[0].matched_ingredients == ["하이알루로닉애씨드", "히알루론산"]


# ── fetch: 성분별 조회 몫 ───────────────────────────────────────


class _QuotaQuery:
    """eq·in_·limit 를 실제로 반영하는 쿼리 대역 — 절단 편중을 재현하려면 필요하다."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._ids: list[int] = []
        self._limit: int | None = None

    def select(self, *_) -> "_QuotaQuery":
        return self

    def eq(self, _column: str, value: int) -> "_QuotaQuery":
        self._ids = [value]
        return self

    def in_(self, _column: str, values: list[int]) -> "_QuotaQuery":
        self._ids = list(values)
        return self

    def order(self, *_, **__) -> "_QuotaQuery":
        return self

    def limit(self, n: int) -> "_QuotaQuery":
        self._limit = n
        return self

    async def execute(self):
        # 실 쿼리도 ingredient_id 순으로 자르므로 절단 편중이 그대로 재현된다.
        matched = sorted(
            (r for r in self._rows if r["ingredient_id"] in self._ids),
            key=lambda r: r["ingredient_id"],
        )
        return type("Result", (), {"data": matched[: self._limit]})()


async def test_each_ingredient_query_carries_its_share_of_the_fetch_limit(monkeypatch):
    """성분별 조회에 상한과 정렬이 붙어야 한다.

    `.limit()` 이 빠지면 정제수·글리세린 같은 초빈출 성분에서 수만 행이 앱으로 실려
    오고, `.order()` 가 빠지면 어떤 행이 잘려 나가는지가 비결정적이 된다.
    """
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1),
        Candidate(name_kor="세라마이드", score=0.9, ingredient_id=2),
    ]
    client = _patch(monkeypatch, tables={"product_ingredients": [_pi_row(1, 100, order_no=1)]})

    await fetch(candidates, {"나이아신아마이드", "세라마이드"}, owned_product_ids=[])

    calls = [(method, args) for name, method, args in client.calls if name == "product_ingredients"]
    assert ("limit", (PRODUCT_FETCH_LIMIT // 2,)) in calls, "상한을 성분 수로 나눠 건다"
    assert ("order", ("order_no",)) in calls, "절단이 결정적이어야 한다 (배합 상위부터)"
    assert [args for method, args in calls if method == "eq"] == [
        ("ingredient_id", 1), ("ingredient_id", 2)
    ], "성분별로 따로 조회한다 (한 쿼리로 묶으면 빈출 성분이 상한을 다 먹는다)"


async def test_fetch_limit_is_split_across_ingredients(monkeypatch):
    """한 성분이 조회 상한을 다 먹으면 나머지 추천 성분은 제품을 한 건도 못 받는다.

    그러면 커버리지 그리디가 첫 제품에서 멈춰(새로 커버할 성분이 없음) 제품이 1개만
    나간다 — 설계 04 §4-1 의 단일 성분 쏠림 해소가 되돌아간다.
    """
    # 성분1(id 작음)이 상한만큼의 제품을 갖고, 성분2는 제품 하나뿐인 상황.
    rows = [_pi_row(1, 1000 + i, order_no=1) for i in range(PRODUCT_FETCH_LIMIT)]
    rows.append(_pi_row(2, 9999, order_no=1))
    query = _QuotaQuery(rows)

    async def _fake():
        return type("Client", (), {"table": lambda _self, _name: query})()

    monkeypatch.setattr(s9_products, "get_supabase", _fake)
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1),
        Candidate(name_kor="세라마이드", score=0.9, ingredient_id=2),
    ]

    result = await fetch(
        candidates, {"나이아신아마이드", "세라마이드"}, owned_product_ids=[]
    )

    assert 9999 in [p.product_id for p in result], "id 큰 성분도 제품을 받아야 한다"


async def test_partial_fetch_failure_keeps_successful_ingredients(monkeypatch):
    """성분 하나의 조회가 실패해도 나머지 성분의 제품은 그대로 나간다."""

    async def _fake():
        return type("Client", (), {"table": lambda _self, _name: _FlakyQuery()})()

    class _FlakyQuery(_QuotaQuery):
        def __init__(self):
            super().__init__([_pi_row(2, 9999, order_no=1)])

        async def execute(self):
            if self._ids == [1]:
                raise RuntimeError("일시 오류")
            return await super().execute()

    monkeypatch.setattr(s9_products, "get_supabase", _fake)
    candidates = [
        Candidate(name_kor="나이아신아마이드", score=0.9, ingredient_id=1),
        Candidate(name_kor="세라마이드", score=0.9, ingredient_id=2),
    ]

    result = await fetch(
        candidates, {"나이아신아마이드", "세라마이드"}, owned_product_ids=[]
    )

    assert [p.product_id for p in result] == [9999]


def test_same_product_name_under_two_ids_is_shown_once():
    """올리브영이 용량·기획 구성만 다른 행을 따로 둔다 — 사용자에겐 같은 제품이다."""

    def _row(product_id: int) -> dict:
        return {
            "ingredient_id": 10,
            "order_no": 1,
            "products": {
                "id": product_id,
                "product_name": f"원본{product_id}",
                "cleaned_product_name": "디오디너리 알파 알부틴 2% + HA",
                "brand": "디오디너리",
                "product_url": f"http://x/{product_id}",
                "main_category": "스킨케어",
            },
        }

    products = _rank([_row(1), _row(2)], {10: {"알파-알부틴"}}, set())

    assert [p.product_name for p in products] == ["디오디너리 알파 알부틴 2% + HA"]
