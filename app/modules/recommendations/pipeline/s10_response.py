"""⑩ 응답 조립 — 서사(answer) + 근거 보기(cases·ingredients, 성분별 경고 귀속).

LLM 은 answer·recommended_names 만 만들고, 근거·경고·모드는 코드가 붙인다.
안전 경고는 flat 배열이 아니라 각 성분(ingredients[])에 귀속한다 (2026-07-22).
확인 불가 응답도 여기서 만든다 (에러 아닌 정형 응답, 설계 01 §2-⑩·§3).
"""

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.modules.recommendations.constants import DISCLAIMER, FUNCTIONAL_NOTICE_BADGES
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.recommendations.pipeline.s6_generation import sanitize_claims
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
    UserContext,
    UserProfile,
)

NO_EVIDENCE_MESSAGE = "입력하신 고민에 대해 신뢰할 만한 추천 근거를 찾지 못했습니다."
NO_CANDIDATE_MESSAGE = "안전성 확인을 통과한 추천 성분을 찾지 못했습니다."
WEAK_EVIDENCE_MESSAGE = "유사 사례가 부족해 일반적인 정보 위주로 안내합니다."

# advisory.code — 근거 상태 사유
CODE_WEAK_EVIDENCE = "weak_evidence"  # ok 지만 근거가 약함 (추천은 함)
CODE_NO_EVIDENCE = "no_evidence"  # 검색 근거가 임계값 미달
CODE_NO_CANDIDATES = "no_candidates"  # 안전 필터 후 후보 0개
CODE_PARTIAL_EVIDENCE = "partial_evidence"  # 일부 고민만 근거 있음


def assemble(
    context: UserContext,
    candidates: list[Candidate],
    narrative: LlmNarrative,
    case_chunks: list[RetrievedChunk],
    efficacy_chunks: list[RetrievedChunk],
    products: list[ProductRecommendation],
    bsti_candidates: list[Candidate] | None = None,
    bsti_products: list[ProductRecommendation] | None = None,
    top_picks: list[tuple[Candidate, str]] | None = None,
    top_products: list[ProductRecommendation] | None = None,
) -> RecommendationResponse:
    """서사에 근거를 붙이고, 안전 경고는 각 성분에 귀속해 최종 응답을 만든다.

    products 는 ⑨에서 조회한 추천 성분 함유 제품 — 코드가 조인한 사실이라 그대로 싣는다.
    bsti_* 는 ⑦ BSTI 가지의 산출로, 고민 추천과 별개 축이라 따로 싣는다(없으면 빈 목록).
    top_* 는 ⑧ 이 두 축을 합쳐 고른 대표(메인 카드)다.
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

    advisory = _evidence_advisory(context, case_chunks, efficacy_chunks)
    by_name = {c.name_kor: c for c in candidates}
    return RecommendationResponse(
        status="ok",
        answer=answer,
        top_ingredients=_top_ingredients(top_picks or [], context),
        top_products=top_products or [],
        cases=_dedup_cases(case_chunks),
        ingredients=_safe_ingredients(efficacy_chunks, by_name, context),
        products=products,
        bsti_ingredients=_bsti_ingredients(bsti_candidates or [], context),
        bsti_products=bsti_products or [],
        advisory=advisory,
        retrieval_mode="vector",
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


def _safe_ingredients(
    efficacy_chunks: list[RetrievedChunk],
    by_name: dict[str, Candidate],
    context: UserContext,
) -> list[IngredientEvidence]:
    """근거 성분 패널을 조립한다 — ⑤ 제거분은 빼고, 중복 회수분은 1회만.

    efficacy_chunks 는 ③ 검색 원본이라 ⑤ 안전 필터가 제거한 성분(임신 금기 레티놀·
    사용제한 금지 성분)의 청크도 그대로 들어 있다. 이를 걸러내지 않으면 제거된 성분이
    경고 없이, 심지어 기능성 배지를 달고 근거 패널에 되살아난다. by_name(=⑤ 통과분)에
    있는 성분만 싣는다. 또 `진정`(홍조·민감)처럼 검색어가 겹치는 고민이 같은 성분을 두 번
    회수하므로 정규화 이름 기준으로 중복을 제거한다 (⑥ `_relevant_chunks` 와 같은 이유).
    """
    seen: set[str] = set()
    result: list[IngredientEvidence] = []
    for chunk in efficacy_chunks:
        raw = chunk.metadata.get("name_kor") or chunk.metadata.get("inci") or ""
        key = normalize_ingredient_name(raw)
        if key not in by_name or key in seen:
            continue
        seen.add(key)
        result.append(_ingredient(chunk, by_name, context))
    return result


def _evidence_from_candidate(
    candidate: Candidate, context: UserContext, match_source: str | None = None
) -> IngredientEvidence:
    """후보에서 곧바로 근거 카드를 만든다 (⑦·⑧ 처럼 검색 청크가 없는 경로).

    ⑤ 안전 필터를 통과한 후보라 경고가 이미 붙어 있다. `주의사항` 타입을 빼는 규칙은
    `_ingredient` 와 같다 — safety_note 로 이미 보여주므로 중복이다.
    """
    name = candidate.name_kor
    return IngredientEvidence(
        name_kor=name,
        inci=candidate.inci,
        similarity=round(candidate.score, 2),
        efficacy=candidate.efficacy,
        safety_note=candidate.safety_note,
        concentration=candidate.recommended_concentration,
        badges=_badges_for(name),
        owned=name in context.owned_ingredients,
        owned_products=context.owned_products_by_ingredient.get(name, []),
        warnings=[w for w in candidate.warnings if w.type != "주의사항"],
        match_source=match_source,
    )


def _bsti_ingredients(
    candidates: list[Candidate], context: UserContext
) -> list[IngredientEvidence]:
    """⑦ BSTI 권장 성분 카드. 검색이 아니라 표 확정 매칭이라 similarity 는 1.0 이다."""
    return [_evidence_from_candidate(c, context) for c in candidates]


def _top_ingredients(
    picks: list[tuple[Candidate, str]], context: UserContext
) -> list[IngredientEvidence]:
    """⑧ 종합 카드 — 출처(`match_source`)를 달아 프론트가 근거를 구분하게 한다.

    similarity 는 출처마다 의미가 다르다(고민=검색 유사도, BSTI=1.0). 섞어서 정렬하지
    않고 ⑧이 정한 순서를 그대로 둔다 — 근거가 겹치는 성분이 위다.
    """
    return [_evidence_from_candidate(c, context, source) for c, source in picks]


def _dedup_cases(case_chunks: list[RetrievedChunk]) -> list[CaseEvidence]:
    """같은 사례가 여러 고민에서 회수되면 1회만 싣는다 (doc_id 기준)."""
    seen: set[str] = set()
    result: list[CaseEvidence] = []
    for chunk in case_chunks:
        if chunk.source.doc_id in seen:
            continue
        seen.add(chunk.source.doc_id)
        result.append(_case(chunk))
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


def _ingredient(
    chunk: RetrievedChunk, by_name: dict[str, Candidate], context: UserContext
) -> IngredientEvidence:
    """성분 근거에 자기 주의(safety_note·농도)·⑤ 규제 경고·배지·보유 표시를 붙인다.

    `주의사항` 타입은 rec_efficacy 원본이라 safety_note 로 대신하고 warnings 에선 뺀다
    (중복 방지). 나머지 규제 경고(임신수유주의·알레르기유발·한도·안전성확인불가·고민상충)만 남긴다.

    조회 키는 반드시 정규화한다 — by_name 키(④의 candidate.name_kor)는 정규화돼 있는데
    metadata 의 name_kor 은 rec_efficacy 원본(개행·괄호 포함)이라, 정규화 없이 조회하면
    더러운 이름 성분(레티놀 등)의 경고·배지·보유 표시가 통째로 빠진다 (names.py 존재 이유).
    """
    m = chunk.metadata
    raw = m.get("name_kor") or m.get("inci") or ""
    key = normalize_ingredient_name(raw)
    cand = by_name.get(key)
    # 표시명도 정규화된 이름을 쓴다 — 개행·괄호가 붙은 원문 노출을 막는다.
    display_name = cand.name_kor if cand else key
    warnings = [w for w in (cand.warnings if cand else []) if w.type != "주의사항"]
    return IngredientEvidence(
        name_kor=display_name,
        inci=m.get("inci"),
        similarity=round(chunk.score, 2),
        efficacy=m.get("efficacy"),
        safety_note=m.get("safety_note"),
        concentration=m.get("recommended_concentration"),
        badges=_badges_for(display_name),
        owned=display_name in context.owned_ingredients,
        owned_products=context.owned_products_by_ingredient.get(display_name, []),
        warnings=warnings,
    )


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
        retrieval_mode="vector",
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
