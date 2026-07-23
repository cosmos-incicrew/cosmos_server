"""⑧ 제품 추천 — "추천 성분을 담은 실제 제품을 찾는다".

LLM 이 최종 추천한 성분(narrative.recommended_names) 중 식약처 ID 가 매핑된 것만 골라,
`product_ingredients` 를 `ingredient_id` 로 역조회해 그 성분을 담은 제품을 찾는다. 커버리지
(추천 성분을 여러 개 담은 제품) 순으로 정렬하고 보유 제품은 뺀다. 설계 01 §2-⑧.

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
    LlmNarrative,
    ProductRecommendation,
)

logger = logging.getLogger(__name__)


async def fetch(
    candidates: list[Candidate],
    narrative: LlmNarrative,
    owned_product_ids: list[int],
) -> list[ProductRecommendation]:
    """추천 성분을 담은 제품을 커버리지 순으로 최대 MAX_RECOMMENDED_PRODUCTS 개 반환한다.

    candidates 는 안전 필터를 통과한 후보(⑤), narrative 는 LLM 서사(⑥)다. 사용자에게
    보인 추천 성분(narrative.recommended_names)과 제품을 일치시키려 그 교집합만 쓴다.
    """
    chosen_names = set(narrative.recommended_names)
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
            "products(id, product_name, brand, product_url, main_category)"
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
    return [acc.to_recommendation() for acc in ranked[:MAX_RECOMMENDED_PRODUCTS]]


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
            product_name=str(p.get("product_name") or ""),
            brand=p.get("brand"),
            product_url=p.get("product_url"),
            main_category=p.get("main_category"),
            matched_ingredients=sorted(self.matched),
        )
