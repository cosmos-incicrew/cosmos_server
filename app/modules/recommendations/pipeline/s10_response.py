"""⑩ 응답 조립 — 서사(answer) + 종합 카드(top_*) + 근거 보기(cases).

LLM 은 answer·recommended_names 만 만들고, 근거·경고·출처는 코드가 붙인다.
안전 경고는 flat 배열이 아니라 각 성분(top_ingredients[])에 귀속한다 (2026-07-22).
확인 불가 응답도 여기서 만든다 (에러 아닌 정형 응답, 설계 01 §2-⑩·§3).
"""

import re

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.modules.recommendations.constants import DISCLAIMER, FUNCTIONAL_NOTICE_BADGES
from app.modules.recommendations.pipeline.s6_generation import sanitize_claims
from app.modules.recommendations.pipeline.s8_top_picks import SOURCE_BOTH, SOURCE_BSTI
from app.modules.recommendations.schemas import (
    Advisory,
    Answer,
    Candidate,
    CaseEvidence,
    IngredientEvidence,
    LlmNarrative,
    ProductRecommendation,
    RecommendationResponse,
    RetrievedChunk,
    TranslatedIngredientText,
    UserContext,
    UserProfile,
)
from app.modules.recommendations.util.display_text import apply_translations, clean_display_text
from app.modules.recommendations.util.ingredient_names import normalize_ingredient_name

NO_EVIDENCE_MESSAGE = "입력하신 고민에 대해 신뢰할 만한 추천 근거를 찾지 못했습니다."
NO_CANDIDATE_MESSAGE = "안전성 확인을 통과한 추천 성분을 찾지 못했습니다."
WEAK_EVIDENCE_MESSAGE = "유사 사례가 부족해 일반적인 정보 위주로 안내합니다."

# advisory.code — 근거 상태 사유
CODE_WEAK_EVIDENCE = "weak_evidence"  # ok 지만 근거가 약함 (추천은 함)
CODE_NO_EVIDENCE = "no_evidence"  # 검색 근거가 임계값 미달
CODE_NO_CANDIDATES = "no_candidates"  # 안전 필터 후 후보 0개
CODE_PARTIAL_EVIDENCE = "partial_evidence"  # 일부 고민만 근거 있음

# rec_efficacy 원본 서술형 주의 — safety_note 로 이미 보여주므로 warnings 에선 뺀다.
# ⑤가 붙이는 경고 타입 문자열이라 리터럴로 흩뿌리면 한쪽만 고쳐도 티가 안 난다.
_REDUNDANT_WARNING_TYPE = "주의사항"

# 선행 서술(`product_traits`)을 버리는 라틴 단어 개수 기준. `락토바이오닉애씨드(Lactobionic
# Acid)` 같은 정상 병기(2개)는 남기고 영어 나열만 걷어내는 선이다.
_LEAD_LATIN_LIMIT = 3
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def assemble(
    context: UserContext,
    narrative: LlmNarrative,
    case_chunks: list[RetrievedChunk],
    efficacy_chunks: list[RetrievedChunk],
    top_picks: list[tuple[Candidate, str]],
    top_products: list[ProductRecommendation],
) -> RecommendationResponse:
    """서사에 근거를 붙이고, 안전 경고는 각 성분에 귀속해 최종 응답을 만든다.

    top_* 는 ⑧ 이 고민 축과 BSTI 축을 합쳐 고른 대표(메인 카드)다. top_picks 가 축
    정보를 함께 들고 오므로 제품 출처도 여기서 되짚어 붙인다.
    """
    # 세 섹션이 모두 공백이면 sanitize 전에 걸러낸다 — sanitize_claims 는 문장이 하나도
    # 안 남으면 정형 문구(_FALLBACK_REASON)로 채워, 원문 공백을 "빈 답변"으로 판별할 수 없다.
    sections = (narrative.cause_analysis, narrative.recommendation, narrative.usage_guide)
    if not any(s.strip() for s in sections):
        return insufficient(context, CODE_NO_CANDIDATES, NO_CANDIDATE_MESSAGE, action="retry_later")
    answer = Answer(
        cause_analysis=sanitize_claims(narrative.cause_analysis).strip(),
        recommendation=sanitize_claims(narrative.recommendation).strip(),
        usage_guide=sanitize_claims(narrative.usage_guide).strip(),
    )

    return RecommendationResponse(
        status="ok",
        answer=answer,
        cases=_dedup_cases(case_chunks),
        top_ingredients=_top_ingredients(top_picks, context, _translations(narrative, top_picks)),
        top_products=_with_match_source(top_products, top_picks),
        advisory=_evidence_advisory(context, case_chunks, efficacy_chunks),
        user_profile=_user_profile(context),
        disclaimer=DISCLAIMER,
    )


def _evidence_advisory(
    context: UserContext,
    case_chunks: list[RetrievedChunk],
    efficacy_chunks: list[RetrievedChunk],
) -> Advisory | None:
    """고민별 근거 유무로 advisory 를 정한다.

    일부 고민만 근거가 없으면 그 고민을 지목(partial_evidence). 케이스가 통째로 없으면
    weak_evidence, 근거가 다 있으면 null.
    """
    covered = {
        c
        for chunk in (*case_chunks, *efficacy_chunks)
        if (c := chunk.metadata.get("concern"))
    }
    missing = [code for code in context.concerns if code not in covered]
    if missing:
        labels = ", ".join(CONCERN_LABEL_BY_CODE.get(code, code) for code in missing)
        return Advisory(
            code=CODE_PARTIAL_EVIDENCE,
            message=f"{labels} 고민은 신뢰할 만한 근거를 찾지 못해 일반 정보 위주로 안내합니다.",
        )
    if not case_chunks:
        return Advisory(code=CODE_WEAK_EVIDENCE, message=WEAK_EVIDENCE_MESSAGE)
    return None


def _translations(
    narrative: LlmNarrative, picks: list[tuple[Candidate, str]]
) -> dict[str, TranslatedIngredientText]:
    """⑥ 번역 결과 중 **실재하는 대표 성분** 것만 남긴다.

    LLM 이 요청하지 않은 성분을 돌려주거나 이름을 지어낼 수 있다 — ⑥의
    `recommended_names` 환각 검사와 같은 원칙으로, 이름이 맞는 항목만 쓴다.
    """
    names = {candidate.name_kor for candidate, _ in picks}
    return {t.name_kor: t for t in narrative.translations if t.name_kor in names}



def _evidence_from_candidate(
    candidate: Candidate,
    context: UserContext,
    match_source: str | None = None,
    translation: TranslatedIngredientText | None = None,
) -> IngredientEvidence:
    """후보에서 곧바로 근거 카드를 만든다 (⑧ 처럼 검색 청크가 없는 경로).

    ⑤ 안전 필터를 통과한 후보라 경고가 이미 붙어 있다.

    similarity 는 `score` 가 아니라 `base_score` 다 — score 에는 ④ 가중치가 섞여 있어
    표시용 유사도가 1 을 넘는다. BSTI 축 성분은 아예 비운다: ⑦이 표 확정 매칭에 준 상수라
    검색 유사도가 아닌데 같은 필드에 실리면 두 축의 수가 같은 뜻으로 읽힌다. `both` 는
    ⑧이 고민 축 후보를 그대로 싣는 경로라 base_score 가 실제 코사인 유사도다.
    """
    name = candidate.name_kor
    return IngredientEvidence(
        name_kor=name,
        # 성분명은 ④가 정규화하는데 INCI 는 원문 그대로였다 — `Niacinamide\n(Vitamin B3)`
        # 처럼 개행이 든 값이 카드에 두 줄로 찍혔다.
        inci=" ".join(candidate.inci.split()) if candidate.inci else None,
        similarity=None if match_source == SOURCE_BSTI else round(candidate.base_score, 2),
        # 원본에 개행·영어 조각이 섞여 있다. ⑥ 번역이 있으면 영어 조각만 제자리 치환하고,
        # 없거나 믿을 수 없으면 삭제 안전망으로 떨어진다 (text 모듈 주석).
        efficacy=_efficacy_text(candidate, translation),
        safety_note=apply_translations(
            candidate.safety_note, translation.safety_note if translation else []
        ),
        concentration=candidate.recommended_concentration,
        badges=_badges_for(name),
        owned=name in context.owned_ingredients,
        owned_products=context.owned_products_by_ingredient.get(name, []),
        # `주의사항` 은 safety_note 로 이미 보여준다.
        warnings=[w for w in candidate.warnings if w.type != _REDUNDANT_WARNING_TYPE],
        match_source=match_source,
    )


def _efficacy_text(
    candidate: Candidate, translation: TranslatedIngredientText | None
) -> str | None:
    """`제품적특성` 을 `효능` 앞에 잇는다.

    원본이 한국어 서술을 두 칸에 나눠 적어, `효능` 만 실으면 "…미백에도 도움을 줄 수
    있습니다" 처럼 앞 문장 없이 시작한다. 임베딩은 이미 두 칸을 합쳐 쓰는데 표시만
    빠져 있었다. 선행 서술은 번역 대상이 아니라 삭제 안전망만 태운다 — 보조 문장이라
    영어면 빼는 편이 낫고, 번역 계약(조각 개수)을 늘리지 않는다.
    """
    lead = clean_display_text(candidate.product_traits)
    # 선행 서술은 문장 부호 없이 영어를 콤마로 나열한 행이 있어(`판테놀`: "humectant and
    # moisturizer Common Application in Creams, …, 피부와 모발에 …") 조각 분할이 한국어와
    # 한 덩어리로 본다. 보조 문장이라 통째로 버리는 편이 영어를 내보내는 것보다 낫다.
    if lead and len(_LATIN_WORD.findall(lead)) >= _LEAD_LATIN_LIMIT:
        lead = None
    body = apply_translations(candidate.efficacy, translation.efficacy if translation else [])
    return " ".join(part for part in (lead, body) if part) or None


def _top_ingredients(
    picks: list[tuple[Candidate, str]],
    context: UserContext,
    translations: dict[str, TranslatedIngredientText],
) -> list[IngredientEvidence]:
    """⑧ 종합 카드 — 출처(`match_source`)를 달아 프론트가 근거를 구분하게 한다.

    similarity 는 고민 축(검색 유사도)에만 있고 BSTI 축은 null 이라 두 축을 섞어 정렬할
    수 없다. ⑧이 정한 순서를 그대로 둔다 — 근거가 겹치는 성분이 위다.
    """
    return [
        _evidence_from_candidate(c, context, source, translations.get(c.name_kor))
        for c, source in picks
    ]


def _with_match_source(
    products: list[ProductRecommendation], picks: list[tuple[Candidate, str]]
) -> list[ProductRecommendation]:
    """제품에도 ⑧ 출처를 단다 — ⑨는 이름 집합만 받아 축을 모르므로 여기서 되짚는다.

    사전을 dict comprehension 으로 만들면 안 된다: ⑧ `_identity` 가 ingredient_id 우선이라
    표시명이 같고 id 가 다른 두 후보가 picks 에 함께 남을 수 있고, ⑧ 순위가
    both→concern→bsti 라 덮어쓰기는 항상 강한 근거를 약한 근거로 깎는 방향이다.

    제품은 부가 정보라 매칭되는 출처가 하나도 없으면 null 로 두고 예외를 올리지 않는다.
    """
    source_by_name: dict[str, str] = {}
    for candidate, source in picks:
        source_by_name.setdefault(candidate.name_kor, source)

    for product in products:
        sources = {
            source_by_name[name]
            for name in product.matched_ingredients
            if name in source_by_name
        }
        if sources:
            product.match_source = SOURCE_BOTH if len(sources) > 1 else sources.pop()
    return products


def _dedup_cases(case_chunks: list[RetrievedChunk]) -> list[CaseEvidence]:
    """같은 사례를 1회만 싣는다 — doc_id 와 **질문 본문** 양쪽으로 본다.

    doc_id 만 보면 놓친다. 코퍼스에 프로필과 추천 성분이 똑같은 별개 상담이 있어
    (질문 문장만 다르다) 사용자에게는 같은 카드가 두 번 보였다 — 실제로 유사도
    0.85·0.84 로 나란히 실렸다. 새 정보가 없는 사례는 한 번만 싣는다.
    """
    seen: set[object] = set()
    result: list[CaseEvidence] = []
    for chunk in case_chunks:
        case = _case(chunk)
        key = (
            case.target_concern,
            case.gender,
            case.age,
            case.skin_type,
            tuple(sorted(case.recommended_ingredients)),
        )
        if case.id in seen or key in seen:
            continue
        seen.add(case.id)
        seen.add(key)
        result.append(case)
    return result


def _case(chunk: RetrievedChunk) -> CaseEvidence:
    m = chunk.metadata
    return CaseEvidence(
        id=chunk.source.doc_id,
        target_concern=m.get("target_concern", ""),
        gender=m.get("gender"),
        age=m.get("age"),
        skin_type=m.get("skin_type"),
        recommended_ingredients=_norm_names(m.get("recommended_ingredients") or []),
        similarity=round(chunk.score, 2),
        question=m.get("question", ""),
        answer=m.get("answer", ""),
    )


def _norm_names(names: list[str]) -> list[str]:
    """근거 표시용 성분명 정규화 + 순서 보존 중복 제거 (개행·괄호 원본 노출 방지)."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in names:
        key = normalize_ingredient_name(raw)
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _badges_for(name_kor: str) -> list[str]:
    """기능성 고시 성분이면 배지를 붙인다 (미백·주름개선 등). 아니면 빈 목록."""
    badge = FUNCTIONAL_NOTICE_BADGES.get(name_kor)
    return [badge] if badge else []


def insufficient(
    context: UserContext, code: str, message: str, action: str | None = None
) -> RecommendationResponse:
    """확인 불가 — 에러가 아니라 정형 응답 (01 §3). advisory 로 사유·안내·행동을 담는다."""
    if action is None:
        action = "retry_with_other_concerns" if context.bsti_type else "take_bsti"
    return RecommendationResponse(
        status="insufficient_evidence",
        answer=None,
        advisory=Advisory(code=code, message=message, action=action),
        user_profile=_user_profile(context),
        disclaimer=DISCLAIMER,
    )


def _user_profile(context: UserContext) -> UserProfile:
    return UserProfile(
        age=context.age,
        gender=context.gender,
        bsti_type=context.bsti_type,
        concerns=context.concerns,
    )
