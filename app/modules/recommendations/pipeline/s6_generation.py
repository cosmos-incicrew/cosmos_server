"""⑥ 생성 — "LLM이 최종 추천글을 쓴다 (딱 한 번)".

여기서만 LLM을 부른다. 두 규칙이 핵심이다 — 근거 없으면 부르지 않고(호출부가 판단),
LLM에는 "이유 쓰기"만 맡기고 정확한 값은 코드가 붙인다(⑩). 설계 01 §2-⑥.
"""

import asyncio
import json
import logging
import re
from typing import Any

from langfuse import get_client, observe

from app.core.gemini import gemini_model_for, get_gemini
from app.modules.recommendations import bsti_traits, errors
from app.modules.recommendations.constants import (
    BANNED_CLAIM_TERMS,
    GENDER_LABELS,
    GENERATION_SEED,
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
    RETRY_SECTION_TEMPLATE,
    TRANSLATION_SECTION_TEMPLATE,
)
from app.modules.recommendations.schemas import (
    Candidate,
    LlmNarrative,
    RetrievedChunk,
    UserContext,
)
from app.modules.recommendations.util.display_text import english_segments

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_FALLBACK_REASON = "검색된 근거를 바탕으로 추천된 성분입니다."


async def generate(
    context: UserContext,
    candidates: list[Candidate],
    chunks: list[RetrievedChunk],
    bsti_candidates: list[Candidate] | None = None,
) -> LlmNarrative:
    """LLM 호출 1회로 ①②③ 서사를 생성한다. 금칙어·후보 밖 추천 시 1회 재생성.

    `bsti_candidates` 는 추천 대상이 아니라 **번역 대상**으로만 쓴다 — BSTI 축 성분도
    카드로 나가므로 영어 원문이 그대로 노출되는 건 같다(병풀추출물이 16타입 중 8개에
    들어 있다). 추천은 여전히 고민 축 후보 안에서만 한다.
    """
    prompt = RECOMMENDATION_USER_TEMPLATE.format(
        min_recommended=MIN_RECOMMENDED,
        recommend_count=_recommend_count(),
        candidate_block=_candidate_block(candidates),
        warning_block=_warning_block(candidates),
        evidence_block=_evidence_block(_relevant_chunks(chunks, candidates)),
        user_block=_user_block(context),
    )
    translation_block = _translation_block([*candidates, *(bsti_candidates or [])])
    if translation_block:
        prompt += TRANSLATION_SECTION_TEMPLATE.format(translation_block=translation_block)
    allowed = {c.name_kor for c in candidates}
    # 후보가 MIN_RECOMMENDED 보다 적을 수 있으니 상한을 씌운다 — 못 채울 수를 요구하면
    # 매번 재생성만 하고 끝난다.
    min_names = min(MIN_RECOMMENDED, len(allowed))
    # 오염(후보밖·금칙어)됐지만 쓸 수 있는 최근 결과. 더 깨끗한 게 안 나오면 이걸 정제해 쓴다.
    tainted: LlmNarrative | None = None
    # 재생성용 교정 지시. 첫 시도엔 비어 있다 — 온도 0·고정 seed 라 같은 프롬프트를 다시
    # 보내면 같은 답이 온다. 실패 사유를 프롬프트에 실어야 재시도가 의미를 갖는다 (04 §9).
    retry_section = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            # 타임아웃이 없으면 Gemini 가 응답하지 않을 때 워커가 무기한 묶인다.
            # 저지연 모델 + 최대 12,000자 근거라 정상 지연은 짧지만, 무응답 방어로 상한을 둔다.
            async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
                out = await _call_gemini(prompt + retry_section, context, candidates)
        except Exception as exc:
            logger.warning("Gemini 호출 실패 (attempt %d/%d)", attempt + 1, _MAX_ATTEMPTS)
            _trace_generation_error(exc)
            if tainted is not None:
                return tainted
            if attempt == _MAX_ATTEMPTS - 1:
                raise errors.llm_upstream_error() from exc
            continue

        # recommended_names 는 내부 검증용이라 후보밖 이름이 downstream 에 새지 않는다
        # (⑩은 안 쓰고, ⑨ 제품은 id 매핑된 후보만 씀). 다만 본문 자유텍스트의 유령 성분은
        # 여기서 못 잡는다 — 한국어 NER 없이는 한계이며, 금칙어는 아래 sanitize 가 방어한다.
        bad_names = [n for n in out.recommended_names if n not in allowed]
        # 너무 적으면(특히 빈 목록) ⑧·⑨이 쓸 성분이 없어 메인 카드·제품이 통째로 빈 채
        # status=ok 로 나간다. 서사 본문엔 성분 설명이 그대로 있어 더 어긋난다.
        too_few = len(out.recommended_names) < min_names
        banned = any(
            _has_banned_claim(t)
            for t in (out.cause_analysis, out.recommendation, out.usage_guide)
        )
        uncovered = _uncovered_concerns(out.recommended_names, candidates, context)
        reasons = _retry_reasons(bad_names, too_few, banned, uncovered, min_names)
        if not reasons:
            return out  # 깨끗한 결과 — 즉시 반환
        logger.info("재생성 — %s (attempt %d)", " / ".join(reasons), attempt)
        retry_section = RETRY_SECTION_TEMPLATE.format(
            reason_block="\n".join(f"- {reason}" for reason in reasons)
        )
        tainted = out  # 마지막 시도까지 안 깨끗하면 이 결과를 ⑩ sanitize_claims 로 정제해 쓴다
    return tainted or LlmNarrative(cause_analysis="", recommendation="", usage_guide="")


@observe(as_type="generation")
async def _call_gemini(
    prompt: str, context: UserContext, candidates: list[Candidate]
) -> LlmNarrative:
    model = gemini_model_for()

    # 프롬프트에는 나이·성별·보유 성분이 들어 있다. 트레이스는 외부 SaaS 에 남으므로
    # 개인 속성을 지운 사본을 기록한다 — 품질 디버깅에 필요한 건 지시문·후보·근거이지
    # 그 사람이 누구인지가 아니다. user_id 도 넣지 않는다.
    # Langfuse v4 에는 트레이스 태그 API 가 없어 모듈 구분은 metadata 로 한다.
    _safe_trace(
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
            "response_schema": LlmNarrative,
            "temperature": GENERATION_TEMPERATURE,
            "seed": GENERATION_SEED,
        },
    )
    raw = response.text or "{}"
    # 출력에도 나이·성별이 자연어로 되풀이될 수 있다(프롬프트가 "고민·나이를 엮어 쓰라"
    # 지시). 입력만 가리고 출력을 그대로 남기면 개인 속성이 트레이스로 우회된다.
    _safe_trace(output=_redact_output(raw, context))
    return LlmNarrative.model_validate(json.loads(raw))


def _redact_output(text: str, context: UserContext) -> str:
    """Langfuse 기록용 출력 사본에서 개인 속성을 마스킹한다 (best-effort).

    사용자에게 가는 원문(response.text)은 그대로 두고 트레이스 사본만 가린다. 나이 숫자·
    성별 라벨을 지운다 — 화장품 서사에서 이 값의 오탐 치환은 드물고, 트레이스 사본이라
    실사용 응답에는 영향이 없다.
    """
    redacted = text
    if context.age is not None:
        redacted = redacted.replace(str(context.age), "[나이]")
    gender = GENDER_LABELS.get(context.gender or "")
    if gender:
        redacted = redacted.replace(gender, "[성별]")
    return redacted


def _safe_trace(**fields: Any) -> None:
    """Langfuse 기록. 트레이싱은 부가 기능이라 SaaS 장애가 생성을 죽이면 안 된다.

    무방어로 두면 Gemini 는 정상인데 Langfuse 지연·오류만으로 generate() 의 except 가
    잡아 재시도·502 로 번진다 (전 사용자 영향).
    """
    try:
        get_client().update_current_generation(**fields)
    except Exception:
        logger.debug("Langfuse 트레이스 기록 실패 — 무시하고 생성 진행", exc_info=True)


def _trace_generation_error(exc: Exception) -> None:
    """LLM 실패를 Langfuse 에 에러로 남긴다. 트레이싱 실패가 요청을 죽이지 않게 한다."""
    try:
        get_client().update_current_generation(level="ERROR", status_message=str(exc))
    except Exception:
        logger.debug("Langfuse 에러 트레이스 기록 실패", exc_info=True)


# ── 프롬프트 블록 조립 ──────────────────────────────────────────


def _candidate_block(candidates: list[Candidate]) -> str:
    """후보를 한 줄에 하나씩, 대응 고민 코드를 붙여 나열한다.

    efficacy 원문에 개행이 섞여 있어(예: "…(미백)수분 유지\\n노화방지…") 그대로 넣으면
    불릿 하나가 여러 줄로 쪼개져 LLM 이 후보 경계를 잘못 읽는다. 한 줄로 눌러 넣는다.

    고민 코드를 붙이는 이유는 "고민마다 최소 하나"(템플릿 요청)를 모델이 판단할 수
    있게 하기 위함이다 — 효능 문장만으로는 어느 고민 몫인지 알 수 없다. 코드는 사용자
    블록의 `고민 코드`와 같은 어휘를 쓴다. 정렬하는 이유는 `candidate.concerns` 가 검색
    결과 도착 순서로 쌓여 그대로 쓰면 프롬프트 문자열이 실행마다 달라지기 때문이다.
    """
    lines = []
    for candidate in candidates:
        efficacy = re.sub(r"\s+", " ", candidate.efficacy).strip() if candidate.efficacy else ""
        concerns = ", ".join(sorted(candidate.concerns))
        lines.append(
            f"- {candidate.name_kor}"
            + (f" [{concerns}]" if concerns else "")
            + (f" — {efficacy}" if efficacy else "")
        )
    return "\n".join(lines)


def _recommend_count() -> str:
    """추천 개수 지시 문구. 두 상수가 같으면 "3~3개"로 렌더돼 지시가 흐려진다.

    범위로 주면 모델이 하한을 고르는 쪽으로 기우는데, 지금은 `MIN_RECOMMENDED` ==
    `MAX_RECOMMENDED`(3, 설계 04 §8)라 애초에 폭이 없다. 두 상수가 다시 벌어지면
    범위 표기로 돌아간다.
    """
    if MIN_RECOMMENDED == MAX_RECOMMENDED:
        return f"정확히 {MAX_RECOMMENDED}"
    return f"{MIN_RECOMMENDED}~{MAX_RECOMMENDED}"


def _warning_block(candidates: list[Candidate]) -> str:
    """후보에 부착된 ⑤ 경고를 한 줄씩 편다. 없으면 '없음'."""
    lines = [f"- {c.name_kor}: {w.text}" for c in candidates for w in c.warnings]
    return "\n".join(lines) if lines else "없음"


def _translation_block(candidates: list[Candidate]) -> str:
    """효능·주의의 **영어 조각만** 번호를 붙여 나열한다. 없으면 빈 문자열.

    조각 단위인 이유는 원문에 한국어와 영어가 섞여 있기 때문이다 — 통째로 영어인 칸만
    다루던 시절엔 혼재 행의 영어 조각이 ⑩에서 삭제됐고, 영어에만 있던 경고(레조시놀의
    민감성 피부 자극)가 그대로 사라졌다. 번호는 ⑩이 되돌려 끼울 자리이며, 개수가
    안 맞으면 ⑩이 번역을 전량 폐기한다.

    한국어 조각을 싣지 않는 이유는 분량이자 안전이다 — 원문 전량을 실으면 근거 분량
    상한(`MAX_EVIDENCE_CHARS`)을 갉아먹고, 모델이 한국어 안전 문구까지 다시 쓴다.

    분할·판정은 ⑩ 이 쓰는 `english_segments` 와 같은 함수다 (text 모듈 주석).
    """
    lines: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.name_kor in seen:
            continue  # 두 축에 겹친 성분 — 같은 원문을 두 번 번역시키지 않는다
        # 라벨을 스키마 필드명과 같게 쓴다 — `효능`/`주의`로 적었더니 모델이 어느 칸에
        # 넣을지 한 번 더 옮겨야 했고, 그 과정에서 원문을 그대로 복사해 돌려줬다.
        fields = [
            (label, segments)
            for label, text in (
                ("efficacy", candidate.efficacy),
                ("safety_note", candidate.safety_note),
            )
            if (segments := english_segments(text))
        ]
        if not fields:
            continue
        seen.add(candidate.name_kor)
        lines.append(f"- {candidate.name_kor}")
        for label, segments in fields:
            for number, segment in enumerate(segments, start=1):
                # 개행을 눌러 넣는 이유는 `_candidate_block` 과 같다 — 항목 경계가 깨진다.
                lines.append(f"  {label} {number}: {re.sub(r'\s+', ' ', segment).strip()}")
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
        것부터 버린다"가 되려면 이 정렬이 전제다. 동점은 doc_id 로 갈라 전순서로 만든다 —
        파이썬 안정 정렬은 동점 구간에서 DB 반환 순서를 그대로 물려받고(RPC 에 tie-break
        가 없다), 그러면 같은 후보로도 프롬프트 문자열과 절단 지점이 실행마다 달라진다.
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
    return sorted(by_doc.values(), key=lambda c: (-c.score, c.source.doc_id))


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


# ── 생성 결과 검증 ──────────────────────────────────────────────


def _uncovered_concerns(
    names: list[str], candidates: list[Candidate], context: UserContext
) -> set[str]:
    """추천이 놓친 고민 코드. 재생성 사유이며, 없으면 빈 집합.

    프롬프트의 "고민마다 최소 하나"는 요청이지 보장이 아니다 — 고민이 홍조·미백 둘인데
    미백 2 + 진정 1 로 나온 실행이 있었다. ④가 후보 단계에서 고민당 슬롯을 예약해 둬도
    ⑥이 한쪽으로 몰아 고르면 그대로 응답이 된다.

    **후보에 실제로 그 고민 성분이 있는 경우만** 요구한다. 검색 결과가 없는 고민까지
    커버를 요구하면 뽑을 후보가 없어 재생성만 반복한다(그 상태는 ⑩의
    `advisory.partial_evidence` 가 따로 알린다). 고민 수가 추천 정원을 넘을 때도 요구하지
    않는다 — 산술적으로 못 채운다.
    """
    coverable = {code for c in candidates for code in c.concerns if code in context.concerns}
    if len(coverable) > MAX_RECOMMENDED:
        return set()
    chosen = set(names)
    covered = {code for c in candidates if c.name_kor in chosen for code in c.concerns}
    return coverable - covered


def _retry_reasons(
    bad_names: list[str],
    too_few: bool,
    banned: bool,
    uncovered: set[str],
    min_names: int,
) -> list[str]:
    """재생성 사유를 모델이 그대로 읽을 문장으로 만든다. 비면 깨끗한 결과다.

    로그와 프롬프트가 **같은 문장**을 쓴다 — 갈라 두면 Langfuse 에서 "왜 다시 불렀나"와
    "무엇을 고치라고 했나"를 대조할 수 없다. 정렬하는 이유는 재현성이다(04 §9): 이름·
    고민 코드가 집합·검색 순서로 들어와 그대로 쓰면 재시도 프롬프트가 실행마다 달라진다.
    """
    reasons: list[str] = []
    if bad_names:
        reasons.append(
            f"후보 목록에 없는 성분을 추천했습니다: {', '.join(sorted(bad_names))}. "
            "후보 성분 목록 안에서만 고르세요."
        )
    if too_few:
        reasons.append(
            f"추천 성분이 {min_names}개보다 적습니다. 후보 목록에서 {min_names}개를 고르세요."
        )
    if banned:
        reasons.append(
            "의약품으로 오인될 표현(질병·증상을 낫게 한다는 서술)이 있습니다. "
            "보습·진정·피부결 개선 같은 화장품 범위로 바꿔 쓰세요."
        )
    if uncovered:
        reasons.append(
            f"고민 {', '.join(sorted(uncovered))} 에 대응하는 성분이 빠졌습니다. "
            "후보 목록에서 그 고민 코드가 붙은 성분을 포함하세요."
        )
    return reasons


def _has_banned_claim(text: str) -> bool:
    return any(term in text for term in BANNED_CLAIM_TERMS)


def sanitize_claims(text: str) -> str:
    """화장품법 금칙어가 든 문장을 덜어낸다. 남는 문장이 없으면 정형 문구로 대체한다.

    재생성으로도 위반이 남을 수 있어 출력 단에서 한 번 더 거른다 (화장품법 §13).
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    kept = [s for s in sentences if not _has_banned_claim(s)]
    return " ".join(kept) if kept else _FALLBACK_REASON
