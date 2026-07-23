"""⑩ 표시 품질 회귀 — 원본 텍스트 정제와 유사도 필드의 의미.

세 가지가 실제 실행 출력에서 새어 나갔다: 효능·주의의 개행이 그대로 JSON 에 실렸고,
한국어 설명 뒤에 영어 원문이 통째로 붙었고, BSTI 표 매칭 상수 1.0 이 검색 유사도와
같은 필드에 실렸다. 표본 문자열은 rec_efficacy 실데이터에서 그대로 가져왔다.
"""

from app.modules.recommendations.pipeline.s8_top_picks import (
    SOURCE_BOTH,
    SOURCE_BSTI,
    SOURCE_CONCERN,
)
from app.modules.recommendations.pipeline.s10_response import (
    _evidence_from_candidate,
    _translations,
    assemble,
)
from app.modules.recommendations.schemas import (
    Candidate,
    ChunkSource,
    LlmNarrative,
    RetrievedChunk,
    TranslatedIngredientText,
    UserContext,
)
from app.modules.recommendations.util.display_text import clean_display_text

# rec_efficacy 실데이터 (헥사펩타이드-2 · 하이알루로닉애씨드 · 세라마이드)
_HEXAPEPTIDE_EFFICACY = "피부 미백 및 항노화 효과\n피부 톤 밝기 및 잔주름 개선"
_HA_EFFICACY = (
    "보습 효과\n피부 탄력 강화\nBinds to water molecules and forms a protective film "
    "on the skin, boosting hydration, improving elasticity, and reducing fine lines."
)
_HA_SAFETY = (
    "대부분 피부 타입에 안전하지만, 드물게 민감한 피부에서는 자극이 있을 수 있습니다.\n"
    "Clinically recognized as highly safe. Non-irritating, non-comedogenic, and suitable "
    "for sensitive and acne-prone skin."
)
_CERAMIDE_SAFETY = (
    "Ceramides are generally considered safe for use in cosmetics at typical "
    "concentrations. Safety assessments report no significant dermal irritation."
)


def _candidate(**over) -> Candidate:
    row = {"name_kor": "세라마이드", "score": 1.0, "base_score": 1.0}
    row.update(over)
    return Candidate(**row)


# ── 문제 4: 개행 ────────────────────────────────────────────────


def test_newlines_collapse_to_one_line():
    """개행이 남으면 카드 한 줄이 두 줄로 깨지고 JSON 에도 \\n 이 그대로 실린다."""
    cleaned = clean_display_text(_HEXAPEPTIDE_EFFICACY)

    assert "\n" not in cleaned
    assert cleaned == "피부 미백 및 항노화 효과 피부 톤 밝기 및 잔주름 개선"


def test_card_text_has_no_newline():
    card = _evidence_from_candidate(
        _candidate(efficacy=_HEXAPEPTIDE_EFFICACY, safety_note=_HA_SAFETY),
        UserContext(user_id="u"),
        SOURCE_CONCERN,
    )

    assert "\n" not in card.efficacy
    assert "\n" not in card.safety_note


# ── 문제 2: 영어 원문 ───────────────────────────────────────────


def test_english_sentences_dropped_when_korean_exists():
    assert clean_display_text(_HA_EFFICACY) == "보습 효과 피부 탄력 강화"
    assert clean_display_text(_HA_SAFETY) == (
        "대부분 피부 타입에 안전하지만, 드물게 민감한 피부에서는 자극이 있을 수 있습니다."
    )


def test_all_english_text_becomes_empty():
    """한국어 전용 서비스에서 읽을 수 없는 문장은 정보가 아니다 — 전부 영어면 비운다.

    실측에서 이 경로가 열렸다: 병풀추출물 주의가 통째로 영어인데 ⑥ 번역이 조각 수를
    못 맞춰 폐기되자 영어가 그대로 사용자에게 나갔다.
    """
    assert clean_display_text(_CERAMIDE_SAFETY) is None


def test_korean_sentence_with_latin_or_units_survives():
    """오탐이 나면 한국어 정보가 사라진다 — 성분명 병기·영문 약어·농도 표기를 지킨다."""
    samples = [
        "락토바이오닉애씨드(Lactobionic Acid)은 각질 제거와 피부 수분 유지에 도움을 줍니다.",
        "민감한 피부에도 비교적 안전하지만, 드물게 patch test 가 권장됩니다.",
        "권장 농도는 0.1~1.0% 입니다.",
        "항산화 효과 (Antioxidant), 항염 효과 (Anti-inflammatory)",
    ]
    for sample in samples:
        assert clean_display_text(sample) == sample


def test_trailing_english_after_korean_sentence_dropped():
    """개행 없이 한 줄 안에서 한국어 문장 뒤에 영어가 붙는 실데이터 형태."""
    text = (
        "비타민 C와 필수 지방산이 풍부하여 피부 재생과 회복을 돕습니다. "
        "Rosehip oil promotes skin regeneration and hydration."
    )

    assert clean_display_text(text) == (
        "비타민 C와 필수 지방산이 풍부하여 피부 재생과 회복을 돕습니다."
    )


def test_none_and_blank_stay_empty():
    assert clean_display_text(None) is None
    assert clean_display_text("   \n ") is None


# ── 문제 3: similarity 의 의미 ──────────────────────────────────


def test_bsti_card_has_no_similarity():
    """⑦의 표 매칭 상수(1.0)를 유사도로 내보내면 BSTI 성분이 가장 정확해 보인다."""
    card = _evidence_from_candidate(_candidate(), UserContext(user_id="u"), SOURCE_BSTI)

    assert card.similarity is None


def test_concern_and_both_cards_keep_similarity():
    """두 축 모두(`both`)여도 ⑧이 싣는 후보는 고민 축이라 코사인 유사도가 실재한다."""
    context = UserContext(user_id="u")
    candidate = _candidate(score=1.07, base_score=0.85)

    assert _evidence_from_candidate(candidate, context, SOURCE_CONCERN).similarity == 0.85
    assert _evidence_from_candidate(candidate, context, SOURCE_BOTH).similarity == 0.85


# ── 문제 2-b: 영어 조각의 제자리 번역 ───────────────────────────
#
# `clean_display_text` 의 삭제는 안전망일 뿐이다. 영어 조각을 지우면 **영어에만 있던
# 정보**가 통째로 사라지므로(레조시놀의 민감성 피부 경고), ⑥이 조각 단위로 번역하고 ⑩이
# 제자리에 끼워 넣는다. 끼워 넣는 조건이 틀어지면 한국어 안전 문구가 조용히 LLM
# 재작성으로 바뀐다 — 그게 여기서 함께 막는 회귀다.

_CERAMIDE_TRANSLATION = "세라마이드는 일반적인 농도에서 화장품에 안전하게 사용됩니다."

# rec_efficacy 실데이터 (레조시놀). 한국어 1조각 + 영어 3조각이 한 칸에 섞여 있다.
# "especially in sensitive skin types" 가 삭제돼 민감성(DSPW) 사용자에게 닿지 않았다.
_RESORCINOL_SAFETY = (
    "다만, 과다 사용 시 피부 자극이나 알레르기 반응을 일으킬 수 있어 주의가 필요합니다. "
    "Considered safe at ≤2% in OTC acne products. "
    "Mild irritation or redness may occur, especially in sensitive skin types. "
    "Overuse should be avoided to prevent skin damage"
)
_RESORCINOL_KOREAN_PART = (
    "다만, 과다 사용 시 피부 자극이나 알레르기 반응을 일으킬 수 있어 주의가 필요합니다."
)
_RESORCINOL_TRANSLATION = [
    "일반의약품 여드름 제품에서 2% 이하 농도는 안전한 것으로 봅니다.",
    "특히 민감성 피부에서는 가벼운 자극이나 홍조가 나타날 수 있습니다.",
    "피부 손상을 막기 위해 과다 사용은 피해야 합니다.",
]


def _narrative(*translations: TranslatedIngredientText) -> LlmNarrative:
    return LlmNarrative(
        cause_analysis="원인",
        recommendation="추천",
        usage_guide="사용법",
        recommended_names=["세라마이드"],
        translations=list(translations),
    )


def test_english_fragments_are_translated_in_place_not_deleted():
    """영어 조각 삭제 = 정보 삭제. 민감성 피부 경고가 영어에만 있었다."""
    card = _evidence_from_candidate(
        _candidate(safety_note=_RESORCINOL_SAFETY),
        UserContext(user_id="u"),
        SOURCE_CONCERN,
        TranslatedIngredientText(name_kor="세라마이드", safety_note=_RESORCINOL_TRANSLATION),
    )

    assert card.safety_note == " ".join([_RESORCINOL_KOREAN_PART, *_RESORCINOL_TRANSLATION])
    assert "민감성 피부" in card.safety_note


def test_korean_fragment_is_never_replaced_by_translation():
    """안전 정보다 — 번역을 적용하더라도 한국어 조각은 원문 그대로여야 한다."""
    card = _evidence_from_candidate(
        _candidate(safety_note=_RESORCINOL_SAFETY),
        UserContext(user_id="u"),
        SOURCE_CONCERN,
        # 모델이 한국어 조각까지 다시 써서 4개를 돌려준 경우 — 개수 불일치로 전량 폐기된다.
        TranslatedIngredientText(
            name_kor="세라마이드",
            safety_note=["LLM 이 다시 쓴 한국어 문장", *_RESORCINOL_TRANSLATION],
        ),
    )

    assert card.safety_note == _RESORCINOL_KOREAN_PART
    assert "LLM 이 다시 쓴" not in card.safety_note


def test_translation_count_mismatch_falls_back_to_deletion():
    """자리가 밀린 채 끼워 넣으면 다른 문장 자리에 엉뚱한 안전 문구가 들어간다."""
    card = _evidence_from_candidate(
        _candidate(safety_note=_RESORCINOL_SAFETY),
        UserContext(user_id="u"),
        SOURCE_CONCERN,
        TranslatedIngredientText(name_kor="세라마이드", safety_note=_RESORCINOL_TRANSLATION[:2]),
    )

    assert card.safety_note == _RESORCINOL_KOREAN_PART


def test_all_english_card_uses_translation():
    """통째로 영어인 칸도 같은 경로다 — 조각이 전부 영어일 뿐이다."""
    card = _evidence_from_candidate(
        _candidate(safety_note=_CERAMIDE_SAFETY),
        UserContext(user_id="u"),
        SOURCE_BSTI,
        TranslatedIngredientText(name_kor="세라마이드", safety_note=[_CERAMIDE_TRANSLATION] * 2),
    )

    assert card.safety_note == f"{_CERAMIDE_TRANSLATION} {_CERAMIDE_TRANSLATION}"


def test_untranslated_echo_is_discarded():
    """모델이 원문을 그대로 복사해 돌려준 적이 실제로 있다 — 영어면 번역본으로 안 쓴다."""
    card = _evidence_from_candidate(
        _candidate(safety_note=_RESORCINOL_SAFETY),
        UserContext(user_id="u"),
        SOURCE_CONCERN,
        TranslatedIngredientText(
            name_kor="세라마이드",
            safety_note=[_RESORCINOL_TRANSLATION[0], "Mild irritation may occur.", ""],
        ),
    )

    assert card.safety_note == _RESORCINOL_KOREAN_PART  # 삭제 안전망으로 떨어진다


def test_missing_translation_drops_english_instead_of_shipping_it():
    """⑥ 실패·누락 시의 안전망 — 영어를 내보내느니 비운다."""
    card = _evidence_from_candidate(
        _candidate(safety_note=_CERAMIDE_SAFETY), UserContext(user_id="u"), SOURCE_BSTI
    )

    assert card.safety_note is None


def test_translation_for_unknown_ingredient_is_ignored():
    """LLM 이 지어낸 성분명의 번역은 버린다 (⑥ `recommended_names` 환각 검사와 같은 원칙)."""
    picks = [(_candidate(safety_note=_CERAMIDE_SAFETY), SOURCE_BSTI)]
    narrative = _narrative(
        TranslatedIngredientText(name_kor="존재하지않는성분", safety_note=["유령 번역"])
    )

    lookup = _translations(narrative, picks)

    assert lookup == {}


def test_translation_reaches_the_card_through_assemble():
    """헬퍼만 검증하면 `assemble` 에서 배선을 지워도 통과한다 — 응답까지 따라간다."""
    picks = [(_candidate(safety_note=_RESORCINOL_SAFETY), SOURCE_CONCERN)]

    response = assemble(
        UserContext(user_id="u"),
        _narrative(
            TranslatedIngredientText(name_kor="세라마이드", safety_note=_RESORCINOL_TRANSLATION)
        ),
        [], [], picks, [],
    )

    assert [i.safety_note for i in response.top_ingredients] == [
        " ".join([_RESORCINOL_KOREAN_PART, *_RESORCINOL_TRANSLATION])
    ]


# ── 원본 중복·분산 서술 ─────────────────────────────────────────


def _case_chunk(doc_id: str, question: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        content=question,
        score=score,
        source=ChunkSource(doc_id=doc_id, title="사례"),
        metadata={"question": question, "target_concern": "미백", "concern": "brightening"},
    )


def test_same_consultation_under_two_case_ids_is_shown_once():
    """rec_cases 에 질문이 같은 행이 85건 있다 — case_id 만 보면 같은 상담이 두 번 실린다."""
    question = "30대 복합성 피부입니다. 색소침착이 고민이에요."
    response = assemble(
        UserContext(user_id="u", concerns=["brightening"]),
        _narrative(),
        [_case_chunk("COT_A", question, 0.85), _case_chunk("COT_B", question, 0.84)],
        [],
        [(_candidate(), SOURCE_CONCERN)],
        [],
    )

    assert [c.id for c in response.cases] == ["COT_A"]


def test_product_traits_leads_the_efficacy_sentence():
    """원본이 서술을 두 칸에 나눠 적어 `효능` 만 실으면 앞 문장 없이 시작한다."""
    card = _evidence_from_candidate(
        _candidate(
            product_traits="주로 염색약과 여드름 치료제에 사용되는 성분입니다.",
            efficacy="멜라닌 합성을 억제하여 피부 미백에도 도움을 줄 수 있습니다.",
        ),
        UserContext(user_id="u"),
        SOURCE_CONCERN,
    )

    assert card.efficacy == (
        "주로 염색약과 여드름 치료제에 사용되는 성분입니다. "
        "멜라닌 합성을 억제하여 피부 미백에도 도움을 줄 수 있습니다."
    )
