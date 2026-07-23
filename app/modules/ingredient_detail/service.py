"""개별 성분 해설·주의사항 로직.

근거 조회(Supabase) → 게이트(확인 불가 판정) → 생성(Gemini) → 출처 검증.
외부 서비스는 app/core의 get_supabase()·get_gemini()로만 접근한다.
llm-rag-rules.md를 예외 없이 따른다.
"""

import asyncio
import re
from typing import Any

from langfuse import get_client, observe

from app.core.gemini import gemini_model_for, get_gemini
from app.core.supabase import get_supabase
from app.modules.ingredient_detail.prompts import (
    COMPARISON_SYSTEM_PROMPT,
    COMPARISON_USER_TEMPLATE,
    EXPLANATION_SYSTEM_PROMPT,
    EXPLANATION_USER_TEMPLATE,
    PRODUCT_SUMMARY_SYSTEM_PROMPT,
    PRODUCT_SUMMARY_USER_TEMPLATE,
)
from app.modules.ingredient_detail.schemas import (
    ComparedProduct,
    ComparisonSummaryResponse,
    IngredientDetailResponse,
    IngredientEvidence,
    IngredientName,
    IngredientNameResponse,
    IngredientPresence,
    ProductSummaryResponse,
    Restriction,
    TopIngredient,
)

_SAFETY_UNKNOWN = "안전성 확인 불가"
_MODULE_TAG = "module:ingredient_detail"
_TOP_INGREDIENT_COUNT = 3  # 대표 성분(배합순 상위) 개수
# 요약 프롬프트에 넣을 성분 수. 전성분이 200개를 넘는 제품도 있는데,
# 2~3문장 요약에 그 전부를 반영할 수는 없다. 배합순 상위가 제품 성격을 좌우한다.
_MAX_SUMMARY_INGREDIENTS = 30

# 제품 요약의 주의 문장에 반드시 넣을 "고위험" 주의사항 키워드.
#
# 특정 대상(임신·수유 중 등)에게 치명적일 수 있어, 배합순과 무관하게
# 전성분에서 찾아 요약에 담는다. 일반적인 자극·알레르기 문구는 여기 넣지 않는다
# (그건 상위 성분 기준으로 따로 요약하고, 상세는 개별 성분 해설에서 본다).
#
# restrictions(배합 한도·금지)는 제외한다 — 제조 기준이지 사용자 주의사항이 아니다.
#
# ⚠️ 한계: safety_note가 자유 텍스트라 키워드로만 판별한다.
#   - 표현이 다르면("가임기", "태아" 등) 놓칠 수 있다
#   - "임신부 사용금지"와 "임신 중 전문가 상담 권고"를 구분하지 못한다
#   - 데이터에 없다고 해서 안전하다는 뜻이 아니다
# 따라서 요약에서도 단정적 표현을 쓰지 않는다(프롬프트 참고).
_HIGH_RISK_KEYWORDS = (
    "임신",
    "임산부",
    "임신부",
    "수유",
    "발암",
    "영유아",
    "어린이",
)
_MIN_COMPARE_PRODUCTS = 2  # 비교는 제품 2개 이상
# 규제 없는 성분의 상한. 전성분을 모두 해설하면 프롬프트가 길고 LLM 비용이 커진다.
# 규제가 있는 성분은 안전 정보라 이 상한을 적용하지 않는다.
_MAX_COMPARE_INGREDIENTS = 12


class IngredientNotFoundError(Exception):
    """요청한 성분이 DB에 존재하지 않는다. 404로 변환한다.

    "성분은 있으나 해설 근거가 부족한" 경우와 구분한다(그쪽은 정상 응답 + 확인 불가).
    """


class EvidenceUnavailableError(Exception):
    """근거 조회(Supabase) 실패. 의존 서비스 문제이므로 503으로 변환한다."""


class GenerationFailedError(Exception):
    """생성(Gemini) 호출 실패. 외부 서비스 문제이므로 502로 변환한다."""


_SOURCE_PATTERNS = [
    re.compile(r"PMID[:\s]*\d+", re.IGNORECASE),
    re.compile(r"DOI[:\s]*[\w./\-]+", re.IGNORECASE),
    re.compile(r"출처[:\s]*([^\n.]+)"),
    re.compile(r"([A-Za-z][\w\-]*-\d{3,})"),
]


async def get_ingredient_detail(ingredient_id: int) -> IngredientDetailResponse:
    """개별 성분 해설·주의사항 생성 (엔드포인트 진입점)."""
    evidence = await _fetch_evidence(ingredient_id)

    # 성분 자체가 없으면 잘못된 요청이다(404). 아래 "근거 부족"과 구분한다.
    if evidence is None:
        raise IngredientNotFoundError(str(ingredient_id))

    # 근거 기반 생성 규칙: 해설 근거가 없으면 생성 호출 자체를 건너뛰고 "확인 불가".
    # 성분은 실재하므로 오류가 아니라 정상 응답이다.
    if not evidence.has_explanation_basis():
        return IngredientDetailResponse(
            status="확인 불가",
            ingredient_id=ingredient_id,
            name=evidence.name_kr,
            reason="해설 근거(효능·특성) 없음",
        )

    safety_unknown = not evidence.has_safety_basis()

    raw_output = await _generate_explanation(evidence, safety_unknown)
    raw_body, raw_safety = _split_explanation(raw_output)
    clean_body, verified = _verify_sources(raw_body, evidence)

    # LLM이 [주의]를 다듬어 주지만, 공식 규제는 원문을 신뢰해 앞에 덧붙인다.
    # (수치·조건이 LLM 정제 과정에서 변형되는 것을 막는다.)
    safety = _finalize_safety(evidence, raw_safety, safety_unknown)

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
    try:
        client = await get_supabase()
        eff_rows = (
            await client.table("rec_efficacy")
            .select("*")
            .eq("ingredient_id", ingredient_id)
            .execute()
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
    except Exception as exc:
        # 근거를 못 읽으면 "근거 없음"과 구분되어야 한다(빈 해설이 아니라 장애).
        raise EvidenceUnavailableError(str(exc)) from exc
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


async def _fetch_evidence_many(
    ingredient_ids: list[int],
) -> tuple[list[IngredientEvidence], int]:
    """여러 성분의 근거를 병렬로 조회한다.

    반환: (근거 목록, 조회 실패 건수). 입력 순서(배합순)를 유지한다.
    성분 하나의 실패가 전체를 막지 않도록 개별적으로 처리한다.
    """

    async def _one(ingredient_id: int) -> IngredientEvidence | None | EvidenceUnavailableError:
        try:
            return await _fetch_evidence(ingredient_id)
        except EvidenceUnavailableError as exc:
            return exc

    results = await asyncio.gather(*(_one(i) for i in ingredient_ids))

    evidences: list[IngredientEvidence] = []
    failed_count = 0
    for result in results:
        if isinstance(result, EvidenceUnavailableError):
            failed_count += 1
        elif result is not None:
            evidences.append(result)
    return evidences, failed_count


def _high_risk_notes(evidences: list[IngredientEvidence]) -> list[str]:
    """특정 대상에게 치명적일 수 있는 주의사항만 추린다.

    전성분의 주의사항을 모두 요약에 넣으면 2~3문장에 담기지 않아 누락이 생긴다.
    임신·수유·금지 등 놓치면 안 되는 것만 골라 요약 근거로 넘긴다.
    """
    notes: list[str] = []
    for evidence in evidences:
        # 배합 규제(restrictions)는 보지 않는다. 제조 기준이라 사용자 주의사항이 아니다.
        text = evidence.safety_note
        if not text:
            continue
        if any(keyword in text for keyword in _HIGH_RISK_KEYWORDS):
            name = evidence.name_kr or str(evidence.ingredient_id)
            notes.append(f"{name}: {text.strip()}")
    return notes


def _split_explanation(raw: str) -> tuple[str, str | None]:
    """LLM 출력을 [해설]/[주의] 두 부분으로 분리한다.

    형식을 지키지 않으면(표시가 없으면) 전체를 본문으로 보고 주의는 None.
    안전 정보를 놓치지 않도록, 파싱 실패 시에도 본문은 살린다.
    """
    body_match = re.search(r"\[해설\]\s*(.*?)(?=\[주의\]|$)", raw, re.DOTALL)
    safety_match = re.search(r"\[주의\]\s*(.*)", raw, re.DOTALL)

    if not body_match:
        # 표시가 없으면 전체를 본문으로.
        return raw.strip(), None

    body = body_match.group(1).strip()
    safety = safety_match.group(1).strip() if safety_match else None

    # "없음"만 있으면 주의사항 없는 것으로 처리.
    if safety and safety.replace(".", "").strip() in ("없음", "없습니다"):
        safety = None
    return body, (safety or None)


def _finalize_safety(
    evidence: IngredientEvidence, refined_safety: str | None, safety_unknown: bool
) -> str | None:
    """최종 주의사항 필드를 만든다.

    공식 규제(restrictions)는 법적 사실이므로 원문을 신뢰해 앞에 붙인다.
    LLM이 정제한 일반 안전성 문구는 그 뒤에 둔다.
    안전성 근거가 아예 없으면 종전과 같이 "안전성 확인 불가".
    """
    parts: list[str] = []

    # 공식 규제는 원문 유지 (수치·조건 변형 방지)
    regulation = _regulation_text(evidence)
    if regulation:
        parts.append(f"[공식 규제] {regulation}")

    if refined_safety:
        parts.append(refined_safety)

    if parts:
        return " ".join(parts)
    if safety_unknown:
        return _SAFETY_UNKNOWN
    return None


def _regulation_text(evidence: IngredientEvidence) -> str | None:
    """공식 규제(restrictions)만 원문 그대로 조립한다.

    법적 사실이라 LLM 정제를 거치지 않는다 — 수치·조건이 바뀌면 안 되기 때문이다.
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
            parts.append(detail)
    return " / ".join(parts) if parts else None


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
    # 안전성 근거가 없으면 아무 줄도 넣지 않는다.
    # "확인 불가"라고 적어 두면 모델이 그걸 소재로 삼아 불안을 주는 문장을 만든다.
    # 안전성 표기는 응답의 safety 필드가 따로 담당한다.
    if not safety_unknown and evidence.safety_note:
        lines.append(f"- 안전성 참고: {evidence.safety_note}")
    return "\n".join(lines)


@observe(as_type="generation")
async def _generate_explanation(evidence: IngredientEvidence, safety_unknown: bool) -> str:
    """근거를 엮어 Gemini로 해설 생성.

    @observe가 Langfuse generation 트레이스를 자동 생성한다.
    module 태그를 붙여 비용·품질을 모듈별로 추적한다.
    """
    evidence_block = _build_evidence_block(evidence, safety_unknown)
    user_prompt = EXPLANATION_USER_TEMPLATE.format(evidence_block=evidence_block)
    model = gemini_model_for()

    langfuse = get_client()
    langfuse.update_current_generation(
        model=model,
        input=user_prompt,
        metadata={"ingredient_id": evidence.ingredient_id, "module": _MODULE_TAG},
    )

    try:
        client = get_gemini()
        response = await client.aio.models.generate_content(
            model=model,
            contents=f"{EXPLANATION_SYSTEM_PROMPT}\n\n{user_prompt}",
        )
    except Exception as exc:
        raise GenerationFailedError(str(exc)) from exc

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

    # 여러 성분의 근거를 병렬로 조회한다(배합순 유지).
    # 전성분이 200개를 넘는 제품도 있어, 순차 조회는 20초 이상 걸린다.
    evidences, failed_count = await _fetch_evidence_many(ingredient_ids)

    # 전부 실패했다면 일시적 장애로 보고 상위에 알린다.
    if failed_count and not evidences:
        raise EvidenceUnavailableError("모든 성분 근거 조회 실패")

    # 요청한 성분이 하나도 DB에 없으면 잘못된 요청이다(404).
    if not evidences:
        raise IngredientNotFoundError(", ".join(str(i) for i in ingredient_ids))

    # 성분은 있으나 해설 근거가 부족한 경우는 정상 응답 + 확인 불가.
    if not any(ev.has_explanation_basis() for ev in evidences):
        return ProductSummaryResponse(status="확인 불가", reason="제품 성분 근거 없음")

    # 대표 성분: 배합순(입력 순서) 상위 N개
    top = [
        TopIngredient(ingredient_id=ev.ingredient_id, name=ev.name_kr)
        for ev in evidences[:_TOP_INGREDIENT_COUNT]
    ]

    # 요약 생성에는 배합순 상위 성분만 쓴다. 200개가 넘는 전성분을 모두 넣어도
    # 2~3문장 요약에 반영되지 않고, 입력 토큰만 늘어난다.
    summary_evidences = evidences[:_MAX_SUMMARY_INGREDIENTS]

    # 다만 고위험 주의사항은 배합순과 무관하게 전성분에서 찾는다.
    # 배합량이 적어도 임신부 금기 성분이라면 알려야 한다.
    high_risk = _high_risk_notes(evidences)

    # 주의 성분 개수는 전성분 기준으로 센다(요약 근거는 상위 30개만 쓰더라도).
    caution_count = sum(
        1 for ev in evidences if ev.safety_note or any(r.has_content() for r in ev.restrictions)
    )

    raw_summary = await _generate_product_summary(summary_evidences, high_risk, caution_count)
    clean_summary, verified = _verify_sources_multi(raw_summary, summary_evidences)

    return ProductSummaryResponse(
        status="ok",
        top_ingredients=top,
        summary=clean_summary,
        source_verified=verified,
    )


def _build_product_evidence_block(
    evidences: list[IngredientEvidence], caution_count: int | None = None
) -> str:
    """여러 성분의 근거를 제품 요약용 블록으로 조립.

    성분별 안전성 문구는 넣지 않는다. 성분마다 "민감성 피부에 자극 가능" 같은
    문구가 붙어 있는데, 이를 근거로 주면 모델이 성분을 하나씩 나열해
    요약이 아니라 목록이 된다. 상세 주의사항은 개별 성분 해설에서 확인한다.

    대신 주의 성분이 몇 개인지만 알려, 요약 수준으로 언급하게 한다.
    """
    blocks: list[str] = []
    if caution_count is None:
        caution_count = sum(
            1 for ev in evidences if ev.safety_note or any(r.has_content() for r in ev.restrictions)
        )

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
        if lines:
            blocks.append(f"- {ev.name_kr or ev.ingredient_id}\n" + "\n".join(lines))

    block = "\n".join(blocks)
    if caution_count:
        block += (
            f"\n\n[일반 주의사항]\n"
            f"- 위 성분 중 {caution_count}개에 자극·알레르기 관련 참고 문구가 있습니다."
            f" 성분을 나열하지 말고, 민감한 피부는 주의가 필요하다는 정도로만"
            f" 짧게 언급하세요."
        )
    return block


@observe(as_type="generation")
async def _generate_product_summary(
    evidences: list[IngredientEvidence],
    high_risk_notes: list[str] | None = None,
    caution_count: int | None = None,
) -> str:
    """여러 성분 근거를 종합해 제품 요약 생성.

    여러 근거를 종합하는 복합 질의.
    """
    evidence_block = _build_product_evidence_block(evidences, caution_count)
    if high_risk_notes:
        # 배합순 상위가 아니어도 반드시 알려야 하는 주의사항을 별도 구역으로 넣는다.
        joined = "\n".join(f"- {note}" for note in high_risk_notes)
        evidence_block += f"\n\n[특별히 주의가 필요한 성분]\n{joined}"
    user_prompt = PRODUCT_SUMMARY_USER_TEMPLATE.format(evidence_block=evidence_block)
    model = gemini_model_for()

    langfuse = get_client()
    langfuse.update_current_generation(
        model=model,
        input=user_prompt,
        metadata={"ingredient_count": len(evidences), "module": _MODULE_TAG},
    )

    try:
        client = get_gemini()
        response = await client.aio.models.generate_content(
            model=model,
            contents=f"{PRODUCT_SUMMARY_SYSTEM_PROMPT}\n\n{user_prompt}",
        )
    except Exception as exc:
        raise GenerationFailedError(str(exc)) from exc

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


async def get_comparison_summary(
    products: list[ComparedProduct],
    presences: list[IngredientPresence],
) -> ComparisonSummaryResponse:
    """다중 제품 비교 해설 생성 (검색엔진 compare 결과를 자연어로).

    배합 비율은 공개되지 않으므로 효능의 우열은 판단하지 않는다.
    성분 구성의 차이와 주의 성분만 설명한다.
    """
    if len(products) < _MIN_COMPARE_PRODUCTS or not presences:
        return ComparisonSummaryResponse(
            status="확인 불가", reason="비교할 제품 또는 성분 정보 없음"
        )

    highlighted = _select_comparison_ingredients(products, presences)

    # 성분 역할을 설명하려면 효능 근거가 필요하다(compare 응답에는 없다).
    # 성분 수가 많을 수 있으므로 병렬로 조회한다.
    target_ids = [p.ingredient_id for p in highlighted]
    fetched, _ = await _fetch_evidence_many(target_ids)
    evidence_by_id: dict[int, IngredientEvidence] = {ev.ingredient_id: ev for ev in fetched}

    raw_summary = await _generate_comparison_summary(products, highlighted, evidence_by_id)
    clean_summary, verified = _verify_sources_multi(raw_summary, list(evidence_by_id.values()))

    return ComparisonSummaryResponse(
        status="ok",
        summary=clean_summary,
        source_verified=verified,
    )


def _select_comparison_ingredients(
    products: list[ComparedProduct], presences: list[IngredientPresence]
) -> list[IngredientPresence]:
    """해설 대상 성분을 추린다. 전성분을 다 넣으면 프롬프트가 길고 비용도 커진다.

    선별 규칙:
      1. 규제가 있는 성분은 상한과 무관하게 모두 포함한다.
         안전 정보가 개수 제한 때문에 누락되면 사용자에게 해가 될 수 있다.
      2. 나머지 상한은 **제품별로 나눠** 배분한다.
         전체에서 일괄로 뽑으면 한 제품의 성분에 몰릴 수 있고,
         그러면 모델이 "다른 제품에는 그런 성분이 없다"고 잘못 추론한다.
      3. 각 제품 몫 안에서는 차이를 드러내는 성분(single·partial)을 우선한다.
      4. 남은 자리는 공통 성분(all)으로 채운다.
    """
    restricted = [p for p in presences if any(r.has_content() for r in p.restrictions)]
    restricted_ids = {p.ingredient_id for p in restricted}
    rest = [p for p in presences if p.ingredient_id not in restricted_ids]

    # 공통 성분은 특정 제품에 귀속되지 않으므로 따로 둔다.
    common = [p for p in rest if p.presence_type == "all"]
    differing = [p for p in rest if p.presence_type != "all"]

    # 차이 성분을 제품별로 묶는다(partial은 여러 제품에 걸치므로 첫 제품 기준).
    by_product: dict[int, list[IngredientPresence]] = {p.id: [] for p in products}
    for presence in differing:
        for product_id in presence.product_ids:
            if product_id in by_product:
                by_product[product_id].append(presence)
                break

    # 상한의 2/3을 차이 설명에 쓰고 제품 수로 나눈다. 나머지는 공통 성분 몫.
    differing_budget = max(1, _MAX_COMPARE_INGREDIENTS * 2 // 3)
    per_product = max(1, differing_budget // max(1, len(products)))

    selected: list[IngredientPresence] = []
    seen: set[int] = set()
    for product_id in by_product:
        for presence in by_product[product_id][:per_product]:
            if presence.ingredient_id not in seen:
                seen.add(presence.ingredient_id)
                selected.append(presence)

    # 남은 자리를 공통 성분으로 채운다.
    remaining = _MAX_COMPARE_INGREDIENTS - len(selected)
    if remaining > 0:
        selected.extend(common[:remaining])

    return restricted + selected


def _build_comparison_evidence_block(
    products: list[ComparedProduct],
    presences: list[IngredientPresence],
    evidence_by_id: dict[int, IngredientEvidence],
) -> str:
    """비교 결과를 프롬프트용 근거 블록으로 조립."""
    name_by_id = {p.id: (p.product_name or f"제품 {p.id}") for p in products}

    lines: list[str] = ["[비교 대상 제품]"]
    lines.extend(f"- {name_by_id[p.id]}" for p in products)

    lines.append("")
    lines.append("[성분별 포함 관계]")
    for presence in presences:
        included = ", ".join(name_by_id.get(pid, f"제품 {pid}") for pid in presence.product_ids)
        scope = {
            "all": "모든 제품에 포함",
            "partial": "일부 제품에만 포함",
            "single": "한 제품에만 포함",
        }.get(presence.presence_type or "", "포함 범위 불명")
        lines.append(f"- {presence.name_kr or presence.ingredient_id} ({scope}: {included})")

        evidence = evidence_by_id.get(presence.ingredient_id)
        if evidence and evidence.efficacy:
            lines.append(f"    역할: {evidence.efficacy}")
        if evidence and evidence.recommended_skin_types:
            lines.append(f"    권장 피부타입: {evidence.recommended_skin_types}")

        for restriction in presence.restrictions:
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
                lines.append(f"    공식 규제(식약처 등): {detail}")

    return "\n".join(lines)


@observe(as_type="generation")
async def _generate_comparison_summary(
    products: list[ComparedProduct],
    presences: list[IngredientPresence],
    evidence_by_id: dict[int, IngredientEvidence],
) -> str:
    """비교 결과를 종합해 해설 생성. 여러 제품·근거 종합이므로 Pro 모델."""
    evidence_block = _build_comparison_evidence_block(products, presences, evidence_by_id)
    user_prompt = COMPARISON_USER_TEMPLATE.format(evidence_block=evidence_block)
    model = gemini_model_for()

    langfuse = get_client()
    langfuse.update_current_generation(
        model=model,
        input=user_prompt,
        metadata={
            "product_count": len(products),
            "ingredient_count": len(presences),
            "module": _MODULE_TAG,
        },
    )

    try:
        client = get_gemini()
        response = await client.aio.models.generate_content(
            model=model,
            contents=f"{COMPARISON_SYSTEM_PROMPT}\n\n{user_prompt}",
        )
    except Exception as exc:
        raise GenerationFailedError(str(exc)) from exc

    output = response.text or ""
    langfuse.update_current_generation(output=output)
    return output


async def get_ingredient_names(ingredient_ids: list[int]) -> IngredientNameResponse:
    """성분 id 목록을 이름 목록으로 변환한다.

    해설을 생성하지 않고 이름만 조회하므로 LLM을 호출하지 않는다.
    프론트가 성분 목록 화면을 그릴 때 사용한다.

    요청 순서(배합순)를 유지하고, DB에 없는 id는 이름을 null로 채워
    프론트가 요청한 개수와 응답 개수를 맞출 수 있게 한다.
    """
    if not ingredient_ids:
        return IngredientNameResponse(ingredients=[])

    # 중복을 제거해 조회하고, 응답은 요청 순서대로 만든다.
    unique_ids = list(dict.fromkeys(ingredient_ids))

    try:
        client = await get_supabase()
        rows = (
            await client.table("ingredients")
            .select("ingredient_id, name_kor, name_eng")
            .in_("ingredient_id", unique_ids)
            .execute()
        )
    except Exception as exc:
        raise EvidenceUnavailableError(str(exc)) from exc

    by_id: dict[int, dict[str, Any]] = {}
    for row in rows.data or []:
        if not isinstance(row, dict):
            continue
        ingredient_id = row.get("ingredient_id")
        if isinstance(ingredient_id, int):
            by_id[ingredient_id] = row

    def _text(value: Any) -> str | None:
        return value if isinstance(value, str) and value.strip() else None

    return IngredientNameResponse(
        ingredients=[
            IngredientName(
                ingredient_id=ingredient_id,
                name_kr=_text(by_id.get(ingredient_id, {}).get("name_kor")),
                name_en=_text(by_id.get(ingredient_id, {}).get("name_eng")),
            )
            for ingredient_id in unique_ids
        ]
    )
