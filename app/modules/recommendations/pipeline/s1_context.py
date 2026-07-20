"""① 컨텍스트 조립 — "이 사람이 누구인지 파악한다".

프로필·BSTI·화장대를 읽어 추천의 입력을 만든다. 설계 01 §2-①.

**우아한 축소가 이 단계의 핵심 불변식이다.** 프로필은 없으면 409로 막지만, BSTI와
화장대는 아직 DB에 테이블조차 없으므로 없으면 해당 요소만 생략하고 진행한다.
안전성 검사와 달리 이 둘은 빠져도 위해가 없기 때문이다.
"""

import asyncio
import logging
from typing import Any

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.core.supabase import get_supabase, rows
from app.modules.recommendations import errors
from app.modules.recommendations.constants import MAX_CONCERNS
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.recommendations.schemas import UserContext

logger = logging.getLogger(__name__)


async def build_context(user_id: str) -> UserContext:
    """프로필·BSTI·화장대를 읽어 추천 입력을 만든다. 온보딩 미완료면 409."""
    try:
        client = await get_supabase()
        profile_rows = rows(
            await client.table("user_profiles")
            .select("age, gender, skin_concerns, is_pregnant, is_nursing")
            .eq("user_id", user_id)  # RLS 미적용(service_role) — user_id 필터 필수
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise errors.db_unavailable() from exc

    profile = profile_rows[0] if profile_rows else {}
    concerns = [c for c in (profile.get("skin_concerns") or []) if c in CONCERN_LABEL_BY_CODE]
    if not profile.get("age") or not concerns:
        raise errors.onboarding_required()

    # 서로 독립이라 순차로 기다릴 이유가 없다. 둘 다 내부에서 예외를 삼키고 빈 값을
    # 돌려주므로 gather 가 중간에 깨지지 않는다.
    (bsti_type, bsti_recommended, bsti_caution), (owned, owned_products) = await asyncio.gather(
        _fetch_bsti(client, user_id), _fetch_shelf(client, user_id)
    )

    return UserContext(
        user_id=user_id,
        age=profile.get("age"),
        gender=profile.get("gender"),
        bsti_type=bsti_type,
        bsti_recommended=bsti_recommended,
        bsti_caution=bsti_caution,
        owned_ingredients=owned,
        owned_products_by_ingredient=owned_products,
        is_pregnant=profile.get("is_pregnant"),
        is_nursing=profile.get("is_nursing"),
        concerns=concerns[:MAX_CONCERNS],  # 검색 호출 상한을 6회로 고정
    )


async def _fetch_bsti(client: Any, user_id: str) -> tuple[str | None, list[str], list[str]]:
    """최근 BSTI 진단과 타입별 권장·기피 성분을 읽는다.

    성분 매핑은 박금별의 BSTI 테이블을 단일 소스로 소비한다(01 §2-①). 설계가 말한
    `bsti_results` 는 존재하지 않고 실제로는 3테이블 조인이며, 그마저 아직 미머지라
    DB에 없다. 실패하면 BSTI 요소만 생략하고 진행한다.
    """
    try:
        diagnoses = rows(
            await client.table("bsti_user_diagnoses")
            .select("result_code")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        type_code = diagnoses[0].get("result_code") if diagnoses else None
        if not type_code:
            return None, [], []

        mappings = rows(
            await client.table("bsti_type_ingredients")
            .select("relation, bsti_ingredients(name_ko)")
            .eq("type_code", type_code)
            .execute()
        )
    except Exception:
        logger.info("BSTI 조회 불가 — BSTI 요소 생략하고 진행", exc_info=True)
        return None, [], []

    recommended: list[str] = []
    caution: list[str] = []
    for row in mappings:
        ingredient = row.get("bsti_ingredients") or {}
        name = ingredient.get("name_ko") if isinstance(ingredient, dict) else None
        if not name:
            continue
        # 후보명과 같은 키로 비교해야 가점·기피 경고가 걸린다
        target = caution if row.get("relation") == "avoid" else recommended
        target.append(normalize_ingredient_name(str(name)))
    return type_code, recommended, caution


async def _fetch_shelf(client: Any, user_id: str) -> tuple[list[str], dict[str, list[str]]]:
    """화장대 보유 성분과 그 성분이 든 보유 제품명을 읽는다.

    `user_shelf` 는 박금별 구현 대기 중이라 컬럼이 확정 전이다(01 §7). 없거나 비어
    있으면 빈 집합으로 진행한다 — 보정(하향·보유 표시)만 생략된다.
    """
    try:
        items = rows(
            await client.table("user_shelf")
            .select("item_type, product_id, ingredient_name")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception:
        logger.info("화장대 조회 불가 — 보유 성분 보정 생략", exc_info=True)
        return [], {}

    owned = {
        normalize_ingredient_name(str(item["ingredient_name"]))
        for item in items
        if item.get("item_type") == "ingredient" and item.get("ingredient_name")
    }

    product_ids = [
        item["product_id"]
        for item in items
        if item.get("item_type") == "product" and item.get("product_id") is not None
    ]
    from_products, products_by_ingredient = await _fetch_product_ingredients(client, product_ids)

    return sorted(owned | from_products), products_by_ingredient


async def _fetch_product_ingredients(
    client: Any, product_ids: list[Any]
) -> tuple[set[str], dict[str, list[str]]]:
    """보유 제품의 전성분을 풀고, 성분별로 어느 제품에서 왔는지 함께 돌려준다.

    (보유 성분 집합, 성분 → 제품명 목록) 을 반환한다 — 호출자의 dict 를 넘겨받아
    안에서 채우면 부수효과가 호출부에서 보이지 않는다.
    """
    if not product_ids:
        return set(), {}
    try:
        ingredient_rows = rows(
            await client.table("product_ingredients")
            .select("raw_name, products(product_name)")
            .in_("product_id", product_ids)
            .execute()
        )
    except Exception:
        logger.info("보유 제품 성분 조회 불가", exc_info=True)
        return set(), {}

    owned: set[str] = set()
    products_by_ingredient: dict[str, list[str]] = {}
    for row in ingredient_rows:
        name = row.get("raw_name")
        if not name:
            continue
        key = normalize_ingredient_name(str(name))
        owned.add(key)
        product = row.get("products") or {}
        product_name = product.get("product_name") if isinstance(product, dict) else None
        if not product_name:
            continue
        # 한 제품이 같은 성분을 두 번 기재하면 제품명이 중복 노출된다.
        names = products_by_ingredient.setdefault(key, [])
        if product_name not in names:
            names.append(product_name)
    return owned, products_by_ingredient
