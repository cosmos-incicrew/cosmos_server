"""① 컨텍스트 조립 테스트 — 온보딩 게이트와 BSTI·화장대의 우아한 축소.

BSTI(`bsti_*`)와 화장대(`user_shelf`)는 아직 DB 에 없다. 없을 때 추천이 죽지 않고
해당 요소만 생략하는지가 이 단계의 핵심 불변식이다.
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
    assert ctx.bsti_recommended == [] and ctx.bsti_caution == []
    assert ctx.owned_ingredients == []


async def test_bsti_splits_recommend_and_avoid(patch_supabase):
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "bsti_user_diagnoses": [{"result_code": "OSPW"}],
            "bsti_type_ingredients": [
                {"relation": "recommend", "bsti_ingredients": {"name_ko": "나이아신아마이드"}},
                {"relation": "avoid", "bsti_ingredients": {"name_ko": "알코올"}},
            ],
        }
    )

    ctx = await context.build_context(_USER)

    assert ctx.bsti_type == "OSPW"
    assert ctx.bsti_recommended == ["나이아신아마이드"]
    assert ctx.bsti_caution == ["알코올"]


async def test_bsti_names_normalized_like_candidates(patch_supabase):
    """후보명은 정규화되므로 BSTI 목록도 같은 키여야 가점·경고가 걸린다."""
    patch_supabase(
        {
            "user_profiles": [_PROFILE],
            "bsti_user_diagnoses": [{"result_code": "OSPW"}],
            "bsti_type_ingredients": [
                {"relation": "avoid", "bsti_ingredients": {"name_ko": "레티놀\n(비타민 A)"}},
            ],
        }
    )

    ctx = await context.build_context(_USER)

    assert ctx.bsti_caution == ["레티놀"]


async def test_no_bsti_diagnosis_keeps_going(patch_supabase):
    patch_supabase({"user_profiles": [_PROFILE], "bsti_user_diagnoses": []})

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
