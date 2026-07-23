"""⑨ 제품 추천 — "추천 성분을 담은 실제 제품을 찾는다".

사용자에게 보인 추천 성분 중 식약처 ID 가 매핑된 것만 골라, `product_ingredients` 를
`ingredient_id` 로 역조회해 그 성분을 담은 제품을 찾는다. 추천 성분을 골고루 커버하는
제품을 그리디로 고르고 보유 제품은 뺀다. 설계 01 §2-⑨ · 04 §4-1.

고민 추천(⑥ LLM 서사의 성분)과 BSTI 추천(⑦ 타입 권장 성분) 양쪽이 이 함수를 쓴다 —
"보인 성분과 제품을 일치시킨다"는 규칙이 같아서 이름 집합만 받는다.

**LLM 미관여** — 성분↔제품은 DB 사실 조인이라 코드가 결정적으로 붙인다("정확한 값은
코드가 붙인다"). 제품은 부가 정보이므로 조회가 실패해도 예외를 올리지 않고 빈 목록을
돌려준다 — 핵심인 성분 추천을 깨뜨리지 않기 위함이다.
"""

import logging
from typing import Any

from app.core.supabase import get_supabase
from app.core.supabase import rows as _narrow
from app.modules.recommendations.constants import (
    MAX_RECOMMENDED_PRODUCTS,
    PRODUCT_FETCH_LIMIT,
)
from app.modules.recommendations.schemas import (
    Candidate,
    ProductRecommendation,
)

logger = logging.getLogger(__name__)


async def fetch(
    candidates: list[Candidate],
    chosen_names: set[str],
    owned_product_ids: list[int],
) -> list[ProductRecommendation]:
    """추천 성분을 담은 제품을 커버리지 순으로 최대 MAX_RECOMMENDED_PRODUCTS 개 반환한다.

    candidates 는 안전 필터를 통과한 후보(⑤), chosen_names 는 사용자에게 실제로 보인
    추천 성분명이다 — 고민 추천은 LLM 서사의 `recommended_names`, BSTI 추천(⑦)은 타입
    권장 성분명이 들어온다. 어느 쪽이든 "보인 성분과 제품을 일치시킨다"는 규칙은 같아
    이름 집합만 받는다(LLM 결합 없음).
    """
    # LLM 추천 성분 중 식약처 ID 가 매핑된 것만. 미매핑(id None)은 조인 키가 없어 제외된다.
    id_to_name = {
        c.ingredient_id: c.name_kor
        for c in candidates
        if c.name_kor in chosen_names and c.ingredient_id is not None
    }
    if not id_to_name:
        return []

    try:
        client = await get_supabase()
        columns = (
            "ingredient_id, order_no, "
            "products(id, product_name, cleaned_product_name, brand, product_url, main_category)"
        )
        pi_rows = _narrow(
            await client.table("product_ingredients")
            .select(columns)
            .in_("ingredient_id", list(id_to_name))
            .order("ingredient_id")  # limit 절단을 결정적으로 만든다
            .limit(PRODUCT_FETCH_LIMIT)
            .execute()
        )
    except Exception:
        # 제품은 부가 정보 — 실패해도 성분 추천은 그대로 나가야 한다.
        logger.warning("제품 추천 조회 실패 — 제품 없이 진행", exc_info=True)
        return []

    return _rank(pi_rows, id_to_name, set(owned_product_ids))


def _rank(
    pi_rows: list[dict[str, Any]],
    id_to_name: dict[int, str],
    owned_product_ids: set[int],
) -> list[ProductRecommendation]:
    """제품별로 담은 추천 성분을 모아 커버리지 순으로 정렬한다.

    같은 제품이 추천 성분을 여러 개 담으면 행이 여러 번 나오므로 product_id 로 묶는다.
    정렬 키는 (매칭 성분 수 내림차순, 최소 order_no 오름차순) — 많이 담을수록, 그리고
    그 성분이 배합 상위(고함량)일수록 위로 온다.
    """
    grouped: dict[int, _ProductAcc] = {}
    for row in pi_rows:
        product = row.get("products")
        if not isinstance(product, dict):
            continue  # 조인 대상 제품이 삭제됐거나 null
        product_id = product.get("id")
        if not isinstance(product_id, int) or product_id in owned_product_ids:
            continue
        ingredient_id = row.get("ingredient_id")
        name = id_to_name.get(ingredient_id) if isinstance(ingredient_id, int) else None
        if not name:
            continue

        acc = grouped.get(product_id)
        if acc is None:
            acc = _ProductAcc(product=product)
            grouped[product_id] = acc
        acc.matched.add(name)
        order_no = row.get("order_no")
        if isinstance(order_no, int):
            acc.min_order_no = min(acc.min_order_no, order_no)

    ranked = sorted(
        grouped.values(),
        key=lambda a: (-len(a.matched), a.min_order_no),
    )
    targets = set(id_to_name.values())
    return [acc.to_recommendation() for acc in _select_by_coverage(ranked, targets)]


def _select_by_coverage(
    ranked: list["_ProductAcc"], targets: set[str]
) -> list["_ProductAcc"]:
    """추천 성분(targets)을 골고루 커버하는 제품을 그리디로 고른다 (설계 04 §4-1).

    아직 안 커버된 추천 성분을 가장 많이 더하는 제품부터 고른다(동점이면 전체 매칭 수,
    배합 상위 순). 새로 커버할 성분이 없으면 멈춘다 — 이미 나온 성분만 담은 제품을 중복
    노출하지 않는다(개수가 MAX_RECOMMENDED_PRODUCTS 미만이 될 수 있다). a·b·c·d 를 한
    제품이 다 담으면 그 하나로, 흩어져 있으면 여러 제품으로 최대한 커버한다.
    """
    picked: list[_ProductAcc] = []
    covered: set[str] = set()
    remaining = list(ranked)
    while remaining and len(picked) < MAX_RECOMMENDED_PRODUCTS:
        best = max(
            remaining,
            key=lambda a: (len(a.matched - covered), len(a.matched), -a.min_order_no),
        )
        if not best.matched - covered:
            break  # 남은 제품이 새 추천 성분을 못 더한다 — 중복 노출 방지
        picked.append(best)
        covered |= best.matched
        remaining.remove(best)
    return picked


class _ProductAcc:
    """제품 하나에 대한 집계 누적기 (product_id 로 묶는 동안 쓰는 가변 상태)."""

    # order_no 가 하나도 없을 때의 최소값 기준. 정렬 시 order_no 있는 제품보다 뒤로 간다.
    _NO_ORDER = 10**9

    def __init__(self, product: dict[str, Any]) -> None:
        self._product = product
        self.matched: set[str] = set()
        self.min_order_no: int = self._NO_ORDER

    def to_recommendation(self) -> ProductRecommendation:
        p = self._product
        return ProductRecommendation(
            product_id=int(p["id"]),
            # 표시명은 정제 컬럼을 쓴다 — 원본(`product_name`)은 크롤링 그대로라
            # `[7월 올영픽] … 기획(+샘플)` 같은 프로모션 문구가 붙어 있다. 정제는 제품
            # 데이터 파이프라인(서지우) 산출물이고, 비어 있을 때만 원본으로 폴백한다.
            product_name=str(p.get("cleaned_product_name") or p.get("product_name") or ""),
            brand=p.get("brand"),
            product_url=p.get("product_url"),
            main_category=p.get("main_category"),
            matched_ingredients=sorted(self.matched),
        )
