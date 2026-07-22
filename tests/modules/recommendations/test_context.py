"""① 컨텍스트 조립 테스트 — 온보딩 게이트와 BSTI·화장대의 우아한 축소.

화장대(`user_shelf`)는 아직 DB 에 없고, BSTI 는 타입 코드만 `user_profiles.bsti_type`
에 있다(성분 매핑은 클라이언트 상수). 없을 때 추천이 죽지 않고 해당 요소만 생략하는지가
이 단계의 핵심 불변식이다.
"""

import pytest
from fastapi import HTTPException

from app.modules.recommendations.constants import MAX_CONCERNS
from app.modules.recommendations.pipeline import s1_context as context

_USER = "11111111-1111-4111-8111-111111111111"

_PROFILE = {
    "age": 32,
    "gender": "female",
    "skin_concerns": ["pores", "sensitivity"],
    "is_pregnant": None,
    "is_nursing": None,
}


# ── 온보딩 게이트 ─────────────────────────────────────────────


async def test_missing_profile_raises_409(patch_supabase):
    patch_supabase({"user_profiles": []})

    with pytest.raises(HTTPException) as exc:
        await context.build_context(_USER)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PROFILE_ONBOARDING_REQUIRED"


async def test_missing_age_raises_409(patch_supabase):
    patch_supabase({"user_profiles": [{**_PROFILE, "age": None}]})

    with pytest.raises(HTTPException) as exc:
        await context.build_context(_USER)

    assert exc.value.status_code == 409


async def test_no_valid_concern_raises_409(patch_supabase):
    """알 수 없는 코드만 있으면 고민이 없는 것과 같다."""
    patch_supabase({"user_profiles": [{**_PROFILE, "skin_concerns": ["없는코드"]}]})

    with pytest.raises(HTTPException) as exc:
        await context.build_context(_USER)

    assert exc.value.status_code == 409


async def test_profile_query_failure_raises_503(patch_supabase):
    patch_supabase(missing={"user_profiles"})

    with pytest.raises(HTTPException) as exc:
        await context.build_context(_USER)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "DB_UNAVAILABLE"


async def test_concerns_capped(patch_supabase):
    """검색 호출 상한을 지키려면 고민이 MAX_CONCERNS 로 잘려야 한다."""
    patch_supabase(
        {
            "user_profiles": [
                {**_PROFILE, "skin_concerns": ["pores", "sensitivity", "acne", "wrinkles"]}
            ]
        }
    )

    ctx = await context.build_context(_USER)

    assert len(ctx.concerns) == MAX_CONCERNS


# ── BSTI·화장대 우아한 축소 ────────────────────────────────────


async def test_missing_bsti_and_shelf_tables_degrade(patch_supabase):
    """두 테이블이 없어도 409/500 이 아니라 해당 요소만 빠진 컨텍스트가 나와야 한다."""
    patch_supabase(
        {"user_profiles": [_PROFILE]},
        missing={"bsti_user_diagnoses", "user_shelf"},
    )

    ctx = await context.build_context(_USER)

    assert ctx.age == 32
    assert ctx.bsti_type is None
    assert ctx.bsti_recommended == []
    assert ctx.owned_ingredients == []


async def test_bsti_type_comes_from_profile(patch_supabase):
    """BSTI 타입의 단일 출처는 user_profiles.bsti_type 이다."""
    patch_supabase({"user_profiles": [{**_PROFILE, "bsti_type": "OSPW"}]})

    ctx = await context.build_context(_USER)

    assert ctx.bsti_type == "OSPW"


async def test_bsti_recommended_filled_from_type(patch_supabase):
    """타입이 있으면 ④ 가점이 쓸 권장 성분이 채워져야 한다.

    비어 있으면 BSTI_BOOST 가 조용히 무력화된다 — 실제로 그런 기간이 있었다.
    """
    patch_supabase({"user_profiles": [{**_PROFILE, "bsti_type": "OSPW"}]})

    ctx = await context.build_context(_USER)

    assert ctx.bsti_type == "OSPW"
    assert "나이아신아마이드" in ctx.bsti_recommended
    assert "살리실릭애씨드" in ctx.bsti_recommended  # DB 표기로 번역돼 나온다


async def test_unknown_bsti_type_degrades(patch_supabase):
    """DB 제약을 통과하지만 표에 없는 코드가 와도 500 이 아니라 빈 목록이다."""
    patch_supabase({"user_profiles": [{**_PROFILE, "bsti_type": "OSPX"}]})

    ctx = await context.build_context(_USER)

    assert ctx.bsti_recommended == []


async def test_no_bsti_type_keeps_going(patch_supabase):
    """검사 전 사용자도 추천은 돌아야 한다 — BSTI 요소만 빠진다."""
    patch_supabase({"user_profiles": [{**_PROFILE, "bsti_type": None}]})

    ctx = await context.build_context(_USER)

    assert ctx.bsti_type is None


# ── 화장대 ───────────────────────────────────────────────────


async def test_shelf_direct_ingredient_registration(patch_supabase):
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "user_shelf": [{"item_type": "ingredient", "ingredient_name": "판테놀"}],
        }
    )

    ctx = await context.build_context(_USER)

    assert ctx.owned_ingredients == ["판테놀"]


async def test_shelf_product_resolves_to_ingredients_and_product_names(patch_supabase):
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "user_shelf": [{"item_type": "product", "product_id": 10001}],
            "product_ingredients": [
                {"raw_name": "판테놀", "products": {"product_name": "OO 토너"}},
                {"raw_name": "나이아신아마이드", "products": {"product_name": "OO 토너"}},
            ],
        }
    )

    ctx = await context.build_context(_USER)

    assert set(ctx.owned_ingredients) == {"판테놀", "나이아신아마이드"}
    assert ctx.owned_products_by_ingredient["판테놀"] == ["OO 토너"]


async def test_shelf_row_without_product_id_does_not_crash(patch_supabase):
    """item_type=product 인데 product_id 가 비어 있는 행 하나로 500 이 나던 회귀."""
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "user_shelf": [
                {"item_type": "product", "product_id": None},
                {"item_type": "ingredient", "ingredient_name": "판테놀"},
            ],
        }
    )

    ctx = await context.build_context(_USER)

    assert ctx.owned_ingredients == ["판테놀"]


async def test_duplicate_raw_name_does_not_duplicate_product_name(patch_supabase):
    """한 제품이 같은 성분을 두 번 기재해도 제품명은 한 번만 노출된다."""
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "user_shelf": [{"item_type": "product", "product_id": 10001}],
            "product_ingredients": [
                {"raw_name": "판테놀", "products": {"product_name": "OO 토너"}},
                {"raw_name": "판테놀", "products": {"product_name": "OO 토너"}},
            ],
        }
    )

    ctx = await context.build_context(_USER)

    assert ctx.owned_products_by_ingredient["판테놀"] == ["OO 토너"]


async def test_ingredient_without_product_name_leaves_no_empty_entry(patch_supabase):
    """제품명을 못 얻은 성분은 보유 목록에는 들어가되 빈 제품 목록을 남기지 않는다."""
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "user_shelf": [{"item_type": "product", "product_id": 10001}],
            "product_ingredients": [{"raw_name": "판테놀", "products": None}],
        }
    )

    ctx = await context.build_context(_USER)

    assert ctx.owned_ingredients == ["판테놀"]
    assert "판테놀" not in ctx.owned_products_by_ingredient
