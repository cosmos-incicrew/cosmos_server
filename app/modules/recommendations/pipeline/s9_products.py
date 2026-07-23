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

import asyncio
import logging
from typing import Any

from app.core.supabase import get_supabase, rows
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
    # 보인 추천 성분 중 식약처 ID 가 매핑된 것만. 미매핑(id None)은 조인 키가 없어 제외된다.
    # 한 id 에 이름이 여럿 매달릴 수 있다 — 두 축이 같은 성분을 다른 표시명으로 부르면
    # (`히알루론산`·`하이알루로닉애씨드`) dict[int, str] 은 앞 이름을 덮어 커버 목표에서
    # 지워 버린다.
    id_to_names: dict[int, set[str]] = {}
    for c in candidates:
        if c.name_kor in chosen_names and c.ingredient_id is not None:
            id_to_names.setdefault(c.ingredient_id, set()).add(c.name_kor)
    if not id_to_names:
        return []

    try:
        client = await get_supabase()
    except Exception:
        # 제품은 부가 정보 — 실패해도 성분 추천은 그대로 나가야 한다.
        logger.warning("제품 추천 조회 실패 — 제품 없이 진행", exc_info=True)
        return []

    # 성분별로 몫을 나눠 따로 조회한다. 한 쿼리로 묶으면 함유 제품이 많은 성분 하나가
    # 상한을 다 먹어 나머지 추천 성분은 행을 한 건도 못 받고, 그러면 커버리지 그리디가
    # 첫 제품에서 멈춰 제품이 1개만 나간다 (설계 04 §4-1 단일 성분 쏠림).
    per_ingredient = max(1, PRODUCT_FETCH_LIMIT // len(id_to_names))
    fetched = await asyncio.gather(
        *(_fetch_for_ingredient(client, i, per_ingredient) for i in id_to_names),
        return_exceptions=True,
    )
    failed = [r for r in fetched if isinstance(r, BaseException)]
    if failed:
        logger.warning(
            "제품 추천 일부 조회 실패 (%d/%d) — 성공분만 사용",
            len(failed), len(fetched), exc_info=failed[0],
        )
    pi_rows = [row for result in fetched if isinstance(result, list) for row in result]

    return _rank(pi_rows, id_to_names, set(owned_product_ids))


async def _fetch_for_ingredient(
    client: Any, ingredient_id: int, limit: int
) -> list[dict[str, Any]]:
    """성분 하나의 `product_ingredients` 행. 절단은 배합 상위(작은 order_no)부터 남긴다."""
    columns = (
        "ingredient_id, order_no, "
        "products(id, product_name, cleaned_product_name, brand, product_url, main_category)"
    )
    return rows(
        await client.table("product_ingredients")
        .select(columns)
        .eq("ingredient_id", ingredient_id)
        .order("order_no")  # limit 절단을 결정적으로 만든다
        .limit(limit)
        .execute()
    )


def _rank(
    pi_rows: list[dict[str, Any]],
    id_to_names: dict[int, set[str]],
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
        names = id_to_names.get(ingredient_id) if isinstance(ingredient_id, int) else None
        if not names:
            continue

        acc = grouped.get(product_id)
        if acc is None:
            acc = _ProductAcc(product=product)
            grouped[product_id] = acc
        acc.matched |= names
        order_no = row.get("order_no")
        if isinstance(order_no, int):
            acc.min_order_no = min(acc.min_order_no, order_no)

    ranked = sorted(
        grouped.values(),
        key=lambda a: (-len(a.matched), a.min_order_no),
    )
    targets = {name for names in id_to_names.values() for name in names}
    return [acc.to_recommendation() for acc in _select_by_coverage(ranked, targets)]


def _select_by_coverage(
    ranked: list["_ProductAcc"], targets: set[str]
) -> list["_ProductAcc"]:
    """추천 성분(targets)을 골고루 커버하는 제품을 그리디로 고른다 (설계 04 §4-1).

    아직 안 커버된 추천 성분을 가장 많이 더하는 제품부터 고른다(동점이면 전체 매칭 수,
    배합 상위 순). a·b·c·d 를 한 제품이 다 담으면 그 하나가 먼저, 흩어져 있으면 여러
    제품으로 나눠 커버한다.

    한 바퀴를 다 커버하면 멈추지 않고 `covered` 를 비워 다음 바퀴를 돈다 —
    MAX_RECOMMENDED_PRODUCTS 까지 채우기 위함이다. 멈추면 대표 성분 5개 중 제품이 있는
    성분이 3개뿐이고 그중 둘이 같은 제품에 함께 들어 있을 때 제품이 2개로 끝난다
    (실측 2026-07-23, 설계 04 §4-2). 성분 사전의 49%(2,236개 중 1,098개)가 제품 0건이라
    이 상황은 예외가 아니라 흔한 쪽이다.

    바퀴를 새로 돌아도 순서 규칙은 그대로라 남은 제품 중 커버 폭이 넓은 것부터 채워지고,
    한 성분이 뒤 슬롯을 독식하지 않는다.
    """
    picked: list[_ProductAcc] = []
    covered: set[str] = set()
    # 표시명이 같은 제품은 한 번만. 올리브영은 용량·기획 구성만 다른 행을 따로 두어
    # (`디오디너리 알파 알부틴 2% + HA` 가 goodsNo 만 다르게 두 건) product_id 로만
    # 묶으면 사용자에겐 같은 제품이 두 번 뜬다.
    seen_names: set[str] = set()
    remaining: list[_ProductAcc] = []
    for acc in ranked:
        # 공백을 지운 키로 본다 — `아이소이 모이스춰닥터 …` 와 `아이소이 모이스춰 닥터 …`
        # 처럼 띄어쓰기만 다른 행이 실제로 따로 있다.
        key = "".join(acc.display_name.split())
        if key in seen_names:
            continue
        seen_names.add(key)
        remaining.append(acc)
    while remaining and len(picked) < MAX_RECOMMENDED_PRODUCTS:
        best = max(
            remaining,
            key=lambda a: (len(a.matched - covered), len(a.matched), -a.min_order_no),
        )
        if not best.matched - covered:
            # 남은 제품이 새 성분을 못 더한다 = 한 바퀴 끝. 커버 목표를 되돌려 다음 바퀴로.
            # `matched` 는 빈 적이 없어(매칭이 있어야 누적기가 생긴다) 다음 회차는 반드시
            # 하나를 고른다 — 무한 루프가 되지 않는다.
            covered = set()
            continue
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

    @property
    def display_name(self) -> str:
        p = self._product
        return str(p.get("cleaned_product_name") or p.get("product_name") or "")

    def to_recommendation(self) -> ProductRecommendation:
        p = self._product
        return ProductRecommendation(
            product_id=int(p["id"]),
            # 표시명은 정제 컬럼을 쓴다 — 원본(`product_name`)은 크롤링 그대로라
            # `[7월 올영픽] … 기획(+샘플)` 같은 프로모션 문구가 붙어 있다. 정제는 제품
            # 데이터 파이프라인(서지우) 산출물이고, 비어 있을 때만 원본으로 폴백한다.
            product_name=self.display_name,
            brand=p.get("brand"),
            product_url=p.get("product_url"),
            main_category=p.get("main_category"),
            matched_ingredients=sorted(self.matched),
        )
