"""⑥ 생성 — "LLM이 최종 추천글을 쓴다 (딱 한 번)".

여기서만 LLM을 부른다. 두 규칙이 핵심이다 — 근거 없으면 부르지 않고(호출부가 판단),
LLM에는 "이유 쓰기"만 맡기고 정확한 값은 코드가 붙인다(⑦). 설계 01 §2-⑥.
"""

import asyncio
import json
import logging
import re

from langfuse import get_client, observe

from app.core.gemini import gemini_model_for, get_gemini
from app.modules.recommendations import bsti_traits, errors
from app.modules.recommendations.constants import (
    BANNED_CLAIM_TERMS,
    GENERATION_TEMPERATURE,
    GENERATION_TIMEOUT_SECONDS,
    MAX_CHUNK_CHARS,
    MAX_EVIDENCE_CHARS,
    MAX_RECOMMENDED,
    MIN_RECOMMENDED,
    MODULE_TAG,
)
from app.modules.recommendations.prompts import (
    RECOMMENDATION_SYSTEM_PROMPT,
    RECOMMENDATION_USER_TEMPLATE,
)
from app.modules.recommendations.schemas import (
    Candidate,
    LlmOutput,
    LlmPick,
    RetrievedChunk,
    UserContext,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_FALLBACK_REASON = "검색된 근거를 바탕으로 추천된 성분입니다."


async def generate(
    context: UserContext, candidates: list[Candidate], chunks: list[RetrievedChunk]
) -> list[LlmPick]:
    """LLM 호출 1회. 파싱·검증 실패 시 1회 재시도하고 재실패면 502."""
    prompt = RECOMMENDATION_USER_TEMPLATE.format(
        min_recommended=MIN_RECOMMENDED,
        max_recommended=MAX_RECOMMENDED,
        candidate_block=_candidate_block(candidates),
        evidence_block=_evidence_block(_relevant_chunks(chunks, candidates)),
        user_block=_user_block(context),
    )

    picks: list[LlmPick] = []
    for attempt in range(_MAX_ATTEMPTS):
        try:
            # 타임아웃이 없으면 Gemini 가 응답하지 않을 때 워커가 무기한 묶인다.
            # Pro + 최대 12,000자 근거라 지연이 길고, 재시도까지 하면 두 배가 된다.
            async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
                output = await _call_gemini(prompt, context, candidates)
        except Exception as exc:
            logger.warning("Gemini 호출 실패 (attempt %d/%d)", attempt + 1, _MAX_ATTEMPTS)
            _trace_generation_error(exc)
            if picks:
                # 금칙어 재생성 중 통신 오류가 난 경우다. 1차 결과는 ⑦의 순화를
                # 거치면 그대로 쓸 수 있으므로, 쓸 수 있는 답을 502 로 버리지 않는다.
                logger.info("재생성 실패 — 금칙어를 순화해 1차 결과를 사용")
                return picks
            if attempt == _MAX_ATTEMPTS - 1:
                raise errors.llm_upstream_error() from exc
            continue

        fresh = _reject_hallucinated(output.picks, candidates)
        # 화장품법 위반 문구는 문장 삭제로 순화하지만, 위반이 섞였다는 건 지시가 먹지
        # 않았다는 신호다. 1회는 다시 생성해 온전한 문장을 받아본다 (01 §2-⑥).
        if attempt == 0 and any(_has_banned_claim(p.reason) for p in fresh):
            logger.info("생성 결과에 표시·광고 금칙어 포함 — 1회 재생성")
            picks = fresh
            continue
        # 재생성 결과가 환각으로 전부 걸러졌으면 1차를 되살린다. 1차는 금칙어만
        # 순화하면 쓸 수 있는 답인데, 버리면 사용자는 근거가 있는데도 확인 불가를 받는다.
        if not fresh and picks:
            logger.info("재생성 결과가 모두 후보 밖 — 금칙어를 순화해 1차 결과를 사용")
            return picks
        return fresh
    return picks


@observe(as_type="generation", capture_input=False, capture_output=False)
async def _call_gemini(prompt: str, context: UserContext, candidates: list[Candidate]) -> LlmOutput:
    """여러 근거를 종합하는 추천 최종 합성이라 Pro 를 쓴다 (llm-rag-rules 사용 기준)."""
    model = gemini_model_for(complex_query=True)

    langfuse = get_client()
    # 프롬프트에는 나이·성별·보유 성분이 들어 있다. 트레이스는 외부 SaaS 에 남으므로
    # 개인 속성을 지운 사본을 기록한다 — 품질 디버깅에 필요한 건 지시문·후보·근거이지
    # 그 사람이 누구인지가 아니다. user_id 도 넣지 않는다.
    # Langfuse v4 에는 트레이스 태그 API 가 없어 모듈 구분은 metadata 로 한다.
    langfuse.update_current_generation(
        model=model,
        input=_redact_personal(prompt, context),
        metadata={
            "module": MODULE_TAG,
            "concerns": context.concerns,
            "bsti_type": context.bsti_type,
            "candidate_count": len(candidates),
        },
    )

    client = get_gemini()
    response = await client.aio.models.generate_content(
        model=model,
        contents=f"{RECOMMENDATION_SYSTEM_PROMPT}\n\n{prompt}",
        config={
            "response_mime_type": "application/json",
            "response_schema": LlmOutput,
            "temperature": GENERATION_TEMPERATURE,
        },
    )
    raw = response.text or "{}"
    langfuse.update_current_generation(output=_redact_model_output(raw, context))
    return LlmOutput.model_validate(json.loads(raw))


def _trace_generation_error(exc: Exception) -> None:
    """LLM 실패를 Langfuse 에 에러로 남긴다. 트레이싱 실패가 요청을 죽이지 않게 한다."""
    try:
        get_client().update_current_generation(level="ERROR", status_message=str(exc))
    except Exception:
        logger.debug("Langfuse 에러 트레이스 기록 실패", exc_info=True)


# ── 프롬프트 블록 조립 ──────────────────────────────────────────


def _candidate_block(candidates: list[Candidate]) -> str:
    """후보를 한 줄에 하나씩 나열한다.

    efficacy 원문에 개행이 섞여 있어(예: "…(미백)수분 유지\\n노화방지…") 그대로 넣으면
    불릿 하나가 여러 줄로 쪼개져 LLM 이 후보 경계를 잘못 읽는다. 한 줄로 눌러 넣는다.
    """
    lines = []
    for candidate in candidates:
        efficacy = re.sub(r"\s+", " ", candidate.efficacy).strip() if candidate.efficacy else ""
        lines.append(f"- {candidate.name_kor}" + (f" — {efficacy}" if efficacy else ""))
    return "\n".join(lines)


def _relevant_chunks(
    chunks: list[RetrievedChunk], candidates: list[Candidate]
) -> list[RetrievedChunk]:
    """살아남은 후보의 근거가 된 청크만 남기고, 중복을 걷어 score 순으로 세운다.

    세 가지를 여기서 한다 —
      - ⑤에서 걸러진 성분의 근거는 뺀다. 고를 수 없는 성분을 설명하는 글이 근거로
        들어가기 때문이다. 실측상 24개 중 13개(4,296자)가 그런 청크였다.
      - doc_id 중복을 없앤다. `진정`(홍조·민감)·`탄력`(주름·처짐)처럼 검색어가 겹치는
        고민이 있어 같은 행이 두 번 회수되고, 그대로 두면 같은 글이 프롬프트에 두 번
        실려 분량 상한을 갉아먹는다.
      - score 내림차순으로 세운다. 아래 `_evidence_block` 의 뒤쪽 절단이 "관련성 낮은
        것부터 버린다"가 되려면 이 정렬이 전제다.
    """
    linked = {doc_id for candidate in candidates for doc_id in candidate.source_doc_ids}
    by_doc: dict[str, RetrievedChunk] = {}
    for chunk in chunks:
        doc_id = chunk.source.doc_id
        if doc_id not in linked:
            continue
        existing = by_doc.get(doc_id)
        if existing is None or chunk.score > existing.score:
            by_doc[doc_id] = chunk
    return sorted(by_doc.values(), key=lambda c: c.score, reverse=True)


def _evidence_block(chunks: list[RetrievedChunk]) -> str:
    """근거를 doc_id 와 함께 나열한다. 총량이 상한을 넘으면 뒤쪽부터 버린다.

    `_relevant_chunks` 가 score 순으로 세워 주므로 뒤쪽이 관련성이 낮다. 상한이 없으면
    상담 답변 길이에 따라 프롬프트가 무한정 커지고, 그대로 Pro 모델 입력 비용이 된다
    (llm-rag-rules 비용 보호 — 설계 §2-① 은 호출 횟수만 막고 분량은 안 막았다).
    """
    blocks: list[str] = []
    used = 0
    for chunk in chunks:
        block = f"[doc_id: {chunk.source.doc_id}] {chunk.source.title}\n{_clip(chunk.content)}"
        if used + len(block) > MAX_EVIDENCE_CHARS:
            logger.info("근거 분량 상한 도달 — %d/%d 청크만 사용", len(blocks), len(chunks))
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _clip(content: str) -> str:
    """청크 하나가 근거 전체를 차지하지 않도록 자른다 (최장 2,312자 관측)."""
    if len(content) <= MAX_CHUNK_CHARS:
        return content
    return content[:MAX_CHUNK_CHARS].rstrip() + " …"


def _user_block(context: UserContext) -> str:
    """사용자 유래 값은 지시문과 분리된 데이터 블록으로만 넘긴다 (injection 방어)."""
    return "\n".join(
        [
            f"나이: {context.age}",
            f"성별: {context.gender or '미입력'}",
            f"피부 특성: {bsti_traits.describe(context.bsti_type) or '검사 전'}",
            f"고민 코드: {', '.join(context.concerns)}",
            f"이미 보유한 성분: {', '.join(context.owned_ingredients) or '없음'}",
        ]
    )


def _redact_personal(prompt: str, context: UserContext) -> str:
    """Langfuse 기록용 사본에서 개인 속성 블록을 지운다.

    프롬프트 원문은 Gemini 로 그대로 가고(추천에 필요), 트레이스에만 가린 사본을
    남긴다. `<user_input>` 블록 전체를 통째로 치환해 필드가 늘어도 새지 않게 한다.
    """
    user_block = _user_block(context)
    return prompt.replace(user_block, "[개인 속성 생략 — 트레이스 미기록]")


def _redact_model_output(output: str, context: UserContext) -> str:
    """모델이 개인 속성을 되풀이해도 trace output에는 남기지 않는다."""
    values = {
        context.user_id,
        str(context.age) if context.age is not None else "",
        context.gender or "",
        *context.owned_ingredients,
        *context.owned_products_by_ingredient,
        *(
            product
            for products in context.owned_products_by_ingredient.values()
            for product in products
        ),
    }
    if context.gender == "female":
        values.add("여성")
    elif context.gender == "male":
        values.add("남성")
    if context.is_pregnant:
        values.update({"임신", "임산부"})
    if context.is_nursing:
        values.add("수유")

    redacted = output
    for value in sorted(values - {""}, key=len, reverse=True):
        redacted = redacted.replace(value, "[개인 속성 생략]")
    return redacted


# ── 생성 결과 검증 ──────────────────────────────────────────────


def _reject_hallucinated(picks: list[LlmPick], candidates: list[Candidate]) -> list[LlmPick]:
    """후보 목록에 없는 성분을 골랐으면 버린다 (환각 차단)."""
    allowed = {c.name_kor for c in candidates}
    return [pick for pick in picks if pick.name_kor in allowed][:MAX_RECOMMENDED]


def _has_banned_claim(text: str) -> bool:
    return any(term in text for term in BANNED_CLAIM_TERMS)


def sanitize_claims(text: str) -> str:
    """화장품법 금칙어가 든 문장을 덜어낸다. 남는 문장이 없으면 정형 문구로 대체한다.

    재생성으로도 위반이 남을 수 있어 출력 단에서 한 번 더 거른다 (화장품법 §13).
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    kept = [s for s in sentences if not _has_banned_claim(s)]
    return " ".join(kept) if kept else _FALLBACK_REASON
