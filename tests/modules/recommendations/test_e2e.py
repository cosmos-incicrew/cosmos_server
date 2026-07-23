"""진짜 E2E — s1~s8 을 실제 구현으로 관통시킨다 (Gemini 만 목).

test_orchestration 은 단계를 전부 목으로 두어 배선 순서만 본다("⑤를 지워도 통과"). 이
테스트는 FakeSupabase 로 DB만 대체하고 나머지 단계 로직·단계 간 데이터 계약(③ metadata
키 → ④ 소비 → ⑤ → ⑩ 조립, ⑥ 추천 성분 → ⑨ 제품 역조회)이 실제로 맞물리는지 검증한다.
"""

import pytest

from app.modules.recommendations import service
from app.modules.recommendations.pipeline import (
    s1_context,
    s3_retrieval,
    s4_candidates,
    s5_safety,
    s9_products,
)
from app.modules.recommendations.pipeline import s6_generation as generation
from app.modules.recommendations.schemas import LlmNarrative
from tests.modules.recommendations.conftest import FakeSupabase

_USER = "11111111-1111-4111-8111-111111111111"

# ③이 rec_efficacy 에서 뽑는 성분. ④가 ingredients 로 ID 를 채우고 ⑨이 그 ID 로 제품을 찾는다.
_TABLES = {
    "user_profiles": [{
        "user_id": _USER, "age": 32, "gender": "female",
        "skin_concerns": ["pores"], "is_pregnant": False, "is_nursing": False,
        "bsti_type": None,
    }],
    "user_shelf": [],
    "rec_cases": [{
        "case_id": "CASE_1", "target_concern": "모공", "skin_type": "지성",
        "skin_concerns": ["모공"], "question": "모공이 넓어요", "answer": "나이아신아마이드 권장",
        "cot": ["1단계", "2단계"], "recommended_ingredients": ["나이아신아마이드"],
        "evidence_sources": ["PMID:1"], "gender": "여성", "age": 32,
    }],
    "rec_efficacy": [{
        "id": 10, "inci": "NIACINAMIDE", "name_kor": "나이아신아마이드",
        "efficacy": "피지 조절", "safety_note": None, "recommended_concentration": "2~5%",
        "recommended_skin_types": "지성", "regulation_note": None,
        "reference_source": "PMID:1", "ingredient_id": 1,
    }],
    "ingredients": [
        {"ingredient_id": 1, "name_kor": "나이아신아마이드", "name_eng": "NIACINAMIDE"},
    ],
    "restrictions": [],
    "product_ingredients": [{
        "ingredient_id": 1, "order_no": 1,
        "products": {"id": 100, "product_name": "모공 세럼", "brand": "브랜드",
                     "product_url": "http://x/100", "main_category": "스킨케어"},
    }],
}


@pytest.fixture()
def _wire(monkeypatch):
    """모든 파이프라인 단계의 get_supabase 를 하나의 FakeSupabase 로, Gemini 는 목으로."""
    async def _fake_supabase() -> FakeSupabase:
        return FakeSupabase(_TABLES)

    for module in (s1_context, s3_retrieval, s4_candidates, s5_safety, s9_products):
        monkeypatch.setattr(module, "get_supabase", _fake_supabase)

    async def _fake_embed_query(text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(s3_retrieval, "embed_query", _fake_embed_query)

    async def _fake_gemini(prompt, context, candidates):
        return LlmNarrative(
            cause_analysis="지성 피부의 모공 원인", recommendation="나이아신아마이드를 추천합니다",
            usage_guide="저녁에 사용", recommended_names=["나이아신아마이드"],
        )

    monkeypatch.setattr(generation, "_call_gemini", _fake_gemini)


async def test_full_pipeline_produces_ingredient_and_product(_wire):
    resp = await service.create_recommendations(_USER)

    assert resp.status == "ok"
    assert resp.answer is not None
    # ③→④→⑤→⑩: 검색된 성분이 근거로 조립됐다
    assert any(i.name_kor == "나이아신아마이드" for i in resp.ingredients)
    # 기능성 배지·미보유 표시까지 배선됨 (B4/B5)
    niacin = next(i for i in resp.ingredients if i.name_kor == "나이아신아마이드")
    assert niacin.badges == ["기능성고시_미백"]
    assert niacin.owned is False
    # ⑥ 추천 성분 → ⑨ 제품 역조회: 그 성분을 담은 제품이 붙었다
    assert [p.product_id for p in resp.products] == [100]
    assert resp.products[0].matched_ingredients == ["나이아신아마이드"]


async def test_onboarding_incomplete_raises_409(monkeypatch):
    """나이·고민 없는 프로필은 생성까지 안 가고 409."""
    async def _fake_supabase() -> FakeSupabase:
        return FakeSupabase({"user_profiles": [{"user_id": _USER, "skin_concerns": []}]})

    monkeypatch.setattr(s1_context, "get_supabase", _fake_supabase)

    with pytest.raises(Exception) as exc_info:
        await service.create_recommendations(_USER)
    assert exc_info.value.status_code == 409  # type: ignore[attr-defined]
