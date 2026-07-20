"""⑦ 응답 조립 — "사용자에게 보낼 형태로 다듬는다".

LLM이 지어낼 수 없는 값(ingredient_id·배지·경고·출처)은 전부 여기서 코드가 붙인다.
확인 불가 응답도 여기서 만든다 — 에러가 아니라 정형 응답이다. 설계 01 §2-⑦·§3.
"""

from app.modules.recommendations.constants import DISCLAIMER, FUNCTIONAL_NOTICE_BADGES
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.recommendations.pipeline.s6_generation import sanitize_claims
from app.modules.recommendations.schemas import (
    Candidate,
    ContextUsed,
    LlmPick,
    RecommendationResponse,
    RecommendedIngredient,
    RetrievedChunk,
    Source,
    UserContext,
)

NO_EVIDENCE_MESSAGE = "입력하신 고민에 대해 신뢰할 만한 추천 근거를 찾지 못했습니다."
NO_CANDIDATE_MESSAGE = "안전성 확인을 통과한 추천 성분을 찾지 못했습니다."


def assemble(
    context: UserContext,
    candidates: list[Candidate],
    picks: list[LlmPick],
    chunks: list[RetrievedChunk],
) -> RecommendationResponse:
    """LLM이 고른 성분에 코드가 아는 사실을 붙여 최종 응답을 만든다."""
    by_name = {c.name_kor: c for c in candidates}
    chunk_by_doc = {c.source.doc_id: c for c in chunks}
    owned = set(context.owned_ingredients)

    items = [
        _build_item(pick, by_name[pick.name_kor], chunk_by_doc, context, owned)
        for pick in picks
        if pick.name_kor in by_name
    ]
    if not items:
        return insufficient(context, NO_CANDIDATE_MESSAGE, action="retry_later")

    return RecommendationResponse(
        status="ok",
        recommended_ingredients=items,
        context_used=_context_used(context),
        disclaimer=DISCLAIMER,
    )


def _build_item(
    pick: LlmPick,
    candidate: Candidate,
    chunk_by_doc: dict[str, RetrievedChunk],
    context: UserContext,
    owned: set[str],
) -> RecommendedIngredient:
    badge = FUNCTIONAL_NOTICE_BADGES.get(normalize_ingredient_name(candidate.name_kor))
    return RecommendedIngredient(
        ingredient_id=candidate.ingredient_id,
        name_kor=candidate.name_kor,
        inci=candidate.inci,
        concerns=pick.concerns or candidate.concerns,
        reason=sanitize_claims(pick.reason),
        efficacy=candidate.efficacy,
        badges=[badge] if badge else [],
        owned=candidate.name_kor in owned,
        owned_products=context.owned_products_by_ingredient.get(candidate.name_kor, []),
        warnings=candidate.warnings,
        sources=_sources(pick, candidate, chunk_by_doc),
    )


def _sources(
    pick: LlmPick, candidate: Candidate, chunk_by_doc: dict[str, RetrievedChunk]
) -> list[Source]:
    """LLM이 인용한 doc_id 를 실제 근거 메타데이터로 해석한다.

    인용이 비면 그 후보를 만든 근거로 대신한다 — 출처 없는 추천을 내보내지 않는다.
    """
    cited = [chunk_by_doc[d] for d in pick.cited_doc_ids if d in chunk_by_doc]
    if not cited:
        cited = [chunk_by_doc[d] for d in candidate.source_doc_ids if d in chunk_by_doc]
    return [
        Source(doc_id=c.source.doc_id, title=c.source.title, locator=c.source.locator)
        for c in cited
    ]


def insufficient(
    context: UserContext, message: str, action: str | None = None
) -> RecommendationResponse:
    """확인 불가 — 에러가 아니라 정형 응답이다 (01 §3)."""
    if action is None:
        action = "retry_with_other_concerns" if context.bsti_type else "take_bsti"
    return RecommendationResponse(
        status="insufficient_evidence",
        message=message,
        suggested_action=action,
        context_used=_context_used(context),
        disclaimer=DISCLAIMER,
    )


def _context_used(context: UserContext) -> ContextUsed:
    """프론트 표시·디버깅용으로 어떤 컨텍스트가 쓰였는지 돌려준다."""
    return ContextUsed(
        age=context.age,
        gender=context.gender,
        bsti_type=context.bsti_type,
        concerns=context.concerns,
    )
