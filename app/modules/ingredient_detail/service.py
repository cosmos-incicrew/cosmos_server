"""개별 성분 해설·주의사항 로직.

근거 조회(Supabase) → 게이트(확인 불가 판정) → 생성(Gemini) → 출처 검증.
외부 서비스는 app/core의 get_supabase()·get_gemini()로만 접근한다.
llm-rag-rules.md를 예외 없이 따른다.
"""

import re

from langfuse import get_client, observe

from app.core.gemini import gemini_model_for, get_gemini
from app.core.supabase import get_supabase
from app.modules.ingredient_detail.prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    EXPLANATION_USER_TEMPLATE,
    PRODUCT_SUMMARY_SYSTEM_PROMPT,
    PRODUCT_SUMMARY_USER_TEMPLATE,
)
from app.modules.ingredient_detail.schemas import (
    IngredientDetailResponse,
    IngredientEvidence,
    ProductSummaryResponse,
    Restriction,
    TopIngredient,
)

_SAFETY_UNKNOWN = "안전성 확인 불가"
_MODULE_TAG = "module:ingredient_detail"
_TOP_INGREDIENT_COUNT = 3  # 대표 성분(배합순 상위) 개수

_SOURCE_PATTERNS = [
    re.compile(r"PMID[:\s]*\d+", re.IGNORECASE),
    re.compile(r"DOI[:\s]*[\w./\-]+", re.IGNORECASE),
    re.compile(r"출처[:\s]*([^\n.]+)"),
    re.compile(r"([A-Za-z][\w\-]*-\d{3,})"),
]


async def get_ingredient_detail(ingredient_id: int) -> IngredientDetailResponse:
    """개별 성분 해설·주의사항 생성 (엔드포인트 진입점)."""
    evidence = await _fetch_evidence(ingredient_id)

    # 근거 기반 생성 규칙: 근거 없으면 생성 호출 자체를 건너뛰고 "확인 불가"
    if evidence is None or not evidence.has_explanation_basis():
        reason = "성분 근거 없음" if evidence is None else "해설 근거(효능·특성) 없음"
        return IngredientDetailResponse(
            status="확인 불가", ingredient_id=ingredient_id, reason=reason
        )

    safety = _build_safety_text(evidence)
    safety_unknown = not evidence.has_safety_basis()

    raw_body = await _generate_explanation(evidence, safety_unknown)
    clean_body, verified = _verify_sources(raw_body, evidence)

    return IngredientDetailResponse(
        status="ok",
        ingredient_id=ingredient_id,
        name=evidence.name_kr,
        body=clean_body,
        safety=safety,
        reference_source=evidence.reference_source,
        source_verified=verified,
    )


async def _fetch_evidence(ingredient_id: int) -> IngredientEvidence | None:
    """ingredients + rec_efficacy를 ingredient_id로 조인해 근거 조립."""
    client = await get_supabase()
    eff_rows = (
        await client.table("rec_efficacy").select("*").eq("ingredient_id", ingredient_id).execute()
    )
    ing_rows = (
        await client.table("ingredients")
        .select("origin_definition, name_kor, name_eng")
        .eq("ingredient_id", ingredient_id)
        .execute()
    )
    # 공식 규제(식약처 등). 성분당 여러 건일 수 있다.
    restriction_rows = (
        await client.table("restrictions")
        .select(
            "restriction_id, regulate_type, notice_ingr_name, "
            "provis_atrcl, limit_cond, is_registered_korea"
        )
        .eq("ingredient_id", ingredient_id)
        .execute()
    )
    if not eff_rows.data and not ing_rows.data:
        return None

    eff_raw = eff_rows.data[0] if eff_rows.data else {}
    ing_raw = ing_rows.data[0] if ing_rows.data else {}
    eff: dict[str, object] = eff_raw if isinstance(eff_raw, dict) else {}
    ing: dict[str, object] = ing_raw if isinstance(ing_raw, dict) else {}

    def pick(*values: object) -> str | None:
        for value in values:
            if value is not None:
                return str(value)
        return None

    restrictions = [
        Restriction(
            restriction_id=row.get("restriction_id")
            if isinstance(row.get("restriction_id"), int)
            else None,
            regulate_type=pick(row.get("regulate_type")),
            notice_ingr_name=pick(row.get("notice_ingr_name")),
            provis_atrcl=pick(row.get("provis_atrcl")),
            limit_cond=pick(row.get("limit_cond")),
            is_registered_korea=row.get("is_registered_korea")
            if isinstance(row.get("is_registered_korea"), bool)
            else None,
        )
        for row in (restriction_rows.data or [])
        if isinstance(row, dict)
    ]

    return IngredientEvidence(
        ingredient_id=ingredient_id,
        name_kr=pick(eff.get("name_kr"), ing.get("name_kor")),
        inci=pick(eff.get("inci"), ing.get("name_eng")),
        origin_definition=pick(ing.get("origin_definition")),
        efficacy=pick(eff.get("efficacy")),
        product_traits=pick(eff.get("product_traits")),
        recommended_skin_types=pick(eff.get("recommended_skin_types")),
        properties=pick(eff.get("properties")),
        safety_note=pick(eff.get("safety_note")),
        regulation_note=pick(eff.get("regulation_note")),
        recommended_concentration=pick(eff.get("recommended_concentration")),
        reference_source=pick(eff.get("reference_source")),
        restrictions=restrictions,
    )


def _build_safety_text(evidence: IngredientEvidence) -> str:
    """주의사항 문구 조립. 공식 규제(restrictions)를 우선하고 safety_note를 보조로 붙인다.

    근거가 하나도 없으면 "안전성 확인 불가"로 명시한다(안전하다고 단정하지 않는다).
    """
    parts: list[str] = []
    for restriction in evidence.restrictions:
        if not restriction.has_content():
            continue
        detail = " / ".join(
            text
            for text in (
                restriction.regulate_type,
                restriction.limit_cond,
                restriction.provis_atrcl,
            )
            if text
        )
        if detail:
            parts.append(f"[공식 규제] {detail}")
    if evidence.safety_note:
        parts.append(evidence.safety_note)
    return " ".join(parts) if parts else _SAFETY_UNKNOWN


def _build_evidence_block(evidence: IngredientEvidence, safety_unknown: bool) -> str:
    """근거를 프롬프트용 블록으로 조립. 있는 항목만 포함."""
    lines: list[str] = []
    if evidence.name_kr:
        lines.append(f"- 성분명: {evidence.name_kr}")
    if evidence.efficacy:
        lines.append(f"- 효능: {evidence.efficacy}")
    if evidence.product_traits:
        lines.append(f"- 제품 특성: {evidence.product_traits}")
    if evidence.origin_definition:
        lines.append(f"- 정의·기원: {evidence.origin_definition}")
    if evidence.reference_source:
        lines.append(f"- 출처: {evidence.reference_source}")
    # 공식 규제는 사실이므로 근거에 명확히 포함한다(있으면 반드시 안내되어야 함).
    for restriction in evidence.restrictions:
        if not restriction.has_content():
            continue
        detail = " / ".join(
            text
            for text in (
                restriction.regulate_type,
                restriction.limit_cond,
                restriction.provis_atrcl,
            )
            if text
        )
        if detail:
            lines.append(f"- 공식 규제(식약처 등): {detail}")
    if safety_unknown:
        lines.append("- 안전성: 확인 불가 (안전하다고 단정하지 말 것)")
    elif evidence.safety_note:
        lines.append(f"- 안전성 참고: {evidence.safety_note}")
    return "\n".join(lines)


@observe(as_type="generation")
async def _generate_explanation(evidence: IngredientEvidence, safety_unknown: bool) -> str:
    """근거를 엮어 Gemini로 해설 생성. Flash 기본(단일 근거 생성).

    @observe가 Langfuse generation 트레이스를 자동 생성한다.
    module 태그를 붙여 비용·품질을 모듈별로 추적한다.
    """
    evidence_block = _build_evidence_block(evidence, safety_unknown)
    user_prompt = EXPLANATION_USER_TEMPLATE.format(evidence_block=evidence_block)
    model = gemini_model_for()  # Flash: 단일 성분 해설은 복합 질의 아님

    langfuse = get_client()
    langfuse.update_current_generation(
        model=model,
        input=user_prompt,
        metadata={"ingredient_id": evidence.ingredient_id, "module": _MODULE_TAG},
    )

    client = get_gemini()
    response = await client.aio.models.generate_content(
        model=model,
        contents=f"{EXPLANATION_SYSTEM_PROMPT}\n\n{user_prompt}",
    )
    output = response.text or ""
    langfuse.update_current_generation(output=output)
    return output


def _verify_sources(text: str, evidence: IngredientEvidence) -> tuple[str, bool]:
    """생성문의 인용 출처가 근거에 있는지 대조. 없으면 제거하고 False."""
    cited: list[str] = []
    for pattern in _SOURCE_PATTERNS:
        for match in pattern.findall(text):
            token = match if isinstance(match, str) else match[0]
            if token.strip():
                cited.append(token.strip())
    if not cited:
        return text, True

    ref_tokens = [
        t.strip().lower() for t in (evidence.reference_source or "").split(",") if t.strip()
    ]
    hallucinated = [
        c for c in cited if not any(rt in c.lower() or c.lower() in rt for rt in ref_tokens)
    ]
    if not hallucinated:
        return text, True

    cleaned = text
    for token in hallucinated:
        cleaned = re.sub(r"출처[:\s]*" + re.escape(token), "", cleaned)
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r",\s*([.。])", r"\1", cleaned)
    cleaned = re.sub(r",\s*$", "", cleaned.strip())
    return cleaned.strip(), False


async def get_product_summary(ingredient_ids: list[int]) -> ProductSummaryResponse:
    """제품 요약 생성 (복수 성분 종합).

    제품 전성분 id(배합순)를 받아, 대표 성분(상위 N개)을 뽑고 전체 근거를
    종합해 제품 요약을 생성한다. 개별 성분 해설은 별도(단수 API)에서 처리.
    """
    if not ingredient_ids:
        return ProductSummaryResponse(status="확인 불가", reason="성분 목록 없음")

    # 여러 성분의 근거 조회 (배합순 유지)
    evidences: list[IngredientEvidence] = []
    for iid in ingredient_ids:
        ev = await _fetch_evidence(iid)
        if ev is not None:
            evidences.append(ev)

    if not any(ev.has_explanation_basis() for ev in evidences):
        return ProductSummaryResponse(status="확인 불가", reason="제품 성분 근거 없음")

    # 대표 성분: 배합순(입력 순서) 상위 N개
    top = [
        TopIngredient(ingredient_id=ev.ingredient_id, name=ev.name_kr)
        for ev in evidences[:_TOP_INGREDIENT_COUNT]
    ]

    raw_summary = await _generate_product_summary(evidences)
    clean_summary, verified = _verify_sources_multi(raw_summary, evidences)

    return ProductSummaryResponse(
        status="ok",
        top_ingredients=top,
        summary=clean_summary,
        source_verified=verified,
    )


def _build_product_evidence_block(evidences: list[IngredientEvidence]) -> str:
    """여러 성분의 근거를 제품 요약용 블록으로 조립."""
    blocks: list[str] = []
    for ev in evidences:
        lines: list[str] = []
        if ev.name_kr:
            lines.append(f"  성분명: {ev.name_kr}")
        if ev.efficacy:
            lines.append(f"  효능: {ev.efficacy}")
        if ev.product_traits:
            lines.append(f"  제품 특성: {ev.product_traits}")
        if ev.recommended_skin_types:
            lines.append(f"  권장 피부타입: {ev.recommended_skin_types}")
        if ev.safety_note:
            lines.append(f"  안전성 참고: {ev.safety_note}")
        if ev.regulation_note:
            lines.append(f"  주의·제한: {ev.regulation_note}")
        for restriction in ev.restrictions:
            if not restriction.has_content():
                continue
            detail = " / ".join(
                text
                for text in (
                    restriction.regulate_type,
                    restriction.limit_cond,
                    restriction.provis_atrcl,
                )
                if text
            )
            if detail:
                lines.append(f"  공식 규제(식약처 등): {detail}")
        if lines:
            blocks.append(f"- {ev.name_kr or ev.ingredient_id}\n" + "\n".join(lines))
    return "\n".join(blocks)


@observe(as_type="generation")
async def _generate_product_summary(evidences: list[IngredientEvidence]) -> str:
    """여러 성분 근거를 종합해 제품 요약 생성.

    여러 근거를 종합하는 복합 질의이므로 Pro 모델을 사용한다(llm-rag-rules).
    """
    evidence_block = _build_product_evidence_block(evidences)
    user_prompt = PRODUCT_SUMMARY_USER_TEMPLATE.format(evidence_block=evidence_block)
    model = gemini_model_for(complex_query=True)  # 여러 근거 종합 → Pro

    langfuse = get_client()
    langfuse.update_current_generation(
        model=model,
        input=user_prompt,
        metadata={"ingredient_count": len(evidences), "module": _MODULE_TAG},
    )

    client = get_gemini()
    response = await client.aio.models.generate_content(
        model=model,
        contents=f"{PRODUCT_SUMMARY_SYSTEM_PROMPT}\n\n{user_prompt}",
    )
    output = response.text or ""
    langfuse.update_current_generation(output=output)
    return output


def _verify_sources_multi(text: str, evidences: list[IngredientEvidence]) -> tuple[str, bool]:
    """제품 요약의 출처 검증. 여러 성분의 출처를 합쳐 대조."""
    merged = ", ".join(ev.reference_source for ev in evidences if ev.reference_source)
    merged_evidence = IngredientEvidence(
        ingredient_id=0, name_kr=None, inci=None, reference_source=merged or None
    )
    return _verify_sources(text, merged_evidence)
