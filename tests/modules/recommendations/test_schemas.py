from app.modules.recommendations.schemas import (
    Advisory,
    Answer,
    CaseEvidence,
    IngredientEvidence,
    IngredientWarning,
    LlmNarrative,
    ProductRecommendation,
    RecommendationResponse,
    UserProfile,
)


def test_response_narrative_shape():
    resp = RecommendationResponse(
        status="ok",
        answer=Answer(
            cause_analysis="원인 분석", recommendation="판테놀 추천", usage_guide="사용법"
        ),
        cases=[CaseEvidence(id="c1", target_concern="홍조", gender="여성",
                            age=30, skin_type="건성",
                            recommended_ingredients=["판테놀"], similarity=0.8,
                            question="…", answer="…")],
        top_ingredients=[IngredientEvidence(
            name_kor="판테놀", inci="PANTHENOL", similarity=0.7, efficacy="진정",
            safety_note="고농도 주의", concentration="1~5%",
            warnings=[IngredientWarning(type="알레르기유발", text="첩포 검사 권장")],
            match_source="concern",
        )],
        top_products=[ProductRecommendation(
            product_id=1, product_name="판테놀 크림",
            matched_ingredients=["판테놀"], match_source="concern",
        )],
        advisory=None,
        user_profile=UserProfile(age=30, concerns=["redness"]),
        disclaimer="본 추천은 의학적 진단이 아닌 참고 정보입니다.",
    )
    assert resp.answer.cause_analysis == "원인 분석"
    assert resp.cases[0].recommended_ingredients == ["판테놀"]
    assert resp.top_ingredients[0].safety_note == "고농도 주의"
    assert resp.top_ingredients[0].warnings[0].type == "알레르기유발"
    assert resp.top_products[0].match_source == "concern"
    assert resp.advisory is None


def test_response_top_level_keys_and_order_match_the_contract():
    """프론트가 읽는 키 8개와 그 순서가 계약이다 — 늘거나 순서가 바뀌면 조용히 어긋난다."""
    resp = RecommendationResponse(
        status="ok", user_profile=UserProfile(), disclaimer="d"
    )

    assert list(resp.model_dump()) == [
        "status", "answer", "cases", "top_ingredients", "top_products",
        "advisory", "user_profile", "disclaimer",
    ]


def test_insufficient_shape_has_null_answer():
    resp = RecommendationResponse(
        status="insufficient_evidence",
        answer=None,
        advisory=Advisory(code="no_evidence", message="근거 없음", action="take_bsti"),
        user_profile=UserProfile(concerns=[]),
        disclaimer="d",
    )
    assert resp.answer is None
    assert resp.advisory.code == "no_evidence"
    assert resp.cases == [] and resp.top_ingredients == []


def test_ingredient_evidence_warning_defaults():
    ing = IngredientEvidence(name_kor="판테놀", similarity=0.7)
    assert ing.warnings == [] and ing.safety_note is None


def test_llm_narrative_defaults():
    out = LlmNarrative(cause_analysis="원인", recommendation="추천", usage_guide="사용법")
    assert out.recommended_names == []
