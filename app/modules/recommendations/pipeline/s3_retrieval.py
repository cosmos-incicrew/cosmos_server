"""③ Retrieval — "믿을 수 있는 자료를 찾아온다".

고민별 질의로 rec_cases · rec_efficacy 두 인덱스를 병렬 검색한다 (설계 01 §2-③).

성분 해설(이호영)은 자체 조회를 쓰므로 이 모듈은 추천 전용이다 (공통 유틸 아님).
이 함수는 **자료를 찾아오는 데까지만** 하고 생성·프롬프트·저score 컷은 호출자 몫이다.

  - score 는 0~1 정규화
  - 저score 컷은 여기서 하지 않는다 — 부르는 쪽(service)이 판단
  - 결과 없으면 예외가 아니라 빈 목록

**현재 모드: 키워드 대체 검색.** 벡터 인덱스·검색 RPC(서지우 소유)가 아직 없어 의미
검색 대신 컬럼 필터·ILIKE 로 실제 행을 가져오고 순위에 비례한 가짜 score 를 매긴다.
실검색으로 바꿀 때 이 모듈 내부만 교체하면 되고 호출부는 무변경이다.
"""

import asyncio
import logging
from enum import StrEnum
from typing import Any

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.core.supabase import get_supabase
from app.core.supabase import rows as _narrow
from app.modules.recommendations import errors
from app.modules.recommendations.constants import (
    CASES_TOP_K,
    CONCERN_SEARCH_KEYWORDS,
    EFFICACY_TOP_K,
    MIN_RETRIEVAL_SCORE,
)
from app.modules.recommendations.schemas import ChunkSource, RetrievedChunk

logger = logging.getLogger(__name__)

# ponytail: 벡터 검색 전까지 순위 기반 고정 score. 이 모드의 천장은 명확하다 —
# ILIKE + id 순 limit 이라 "관련성 높은 순"이 아니라 "id 작은 순"이다(주름 검색에
# 레티놀·아데노신이 안 잡힌다). 결정성만 보장하고 관련성은 포기한 상태이며,
# 서지우 RPC 연결 시 실제 코사인 유사도로 교체하고 이 상수를 지운다. 그때 분포가
# 달라지므로 호출자의 임계값(MIN_RETRIEVAL_SCORE)은 Langfuse 트레이스로 재튜닝한다.
_FAKE_TOP_SCORE = 0.9
_FAKE_SCORE_STEP = 0.05
_FAKE_MIN_SCORE = 0.55

# 무필터 fallback 으로 가져온 사례의 score. 이 행들은 고민과의 관련성이 검증되지
# 않았는데(아래 `_search_cases` 참조) 일반 경로와 같은 0.9 를 주면 실제로 관련 있는
# 근거를 밀어내고 프롬프트 앞자리를 차지한다. 임계값은 넘겨 "있는 근거"로는 쓰되
# 항상 맨 뒤에 서도록 최하위 점수를 준다 — ⑥의 분량 절단에서 가장 먼저 버려진다.
_FALLBACK_SCORE = MIN_RETRIEVAL_SCORE


class RetrievalCollection(StrEnum):
    """검색 대상 인덱스. 문자열 오타를 막으려고 Enum 으로 고정한다."""

    REC_CASES = "rec_cases"
    REC_EFFICACY = "rec_efficacy"


class RetrievalError(RuntimeError):
    """검색 실패. 호출자는 이 예외 하나만 잡으면 된다."""


async def retrieve(
    collection: RetrievalCollection,
    query: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """질의로 컬렉션을 검색해 표준 청크 목록을 반환한다.

    filters 키(키워드 모드에서 사용):
      - `concern_labels: list[str]` — rec_cases 의 skin_concerns 배열 겹침 매칭
      - `keywords: list[str]` — rec_efficacy 의 efficacy·name_kr ILIKE 매칭
    """
    filters = filters or {}
    try:
        client = await get_supabase()
        if collection is RetrievalCollection.REC_CASES:
            cases, is_fallback = await _search_cases(client, filters, top_k)
            return [_case_chunk(row, rank, is_fallback) for rank, row in enumerate(cases)]
        efficacy = await _search_efficacy(client, filters, top_k)
        return [_efficacy_chunk(row, rank) for rank, row in enumerate(efficacy)]
    except Exception as exc:  # 실패는 종류를 가리지 않고 하나의 예외로 감싼다
        raise RetrievalError(f"{collection.value} 검색 실패: {exc}") from exc


def _rank_score(rank: int) -> float:
    return max(_FAKE_MIN_SCORE, _FAKE_TOP_SCORE - rank * _FAKE_SCORE_STEP)


async def _search_cases(
    client: Any, filters: dict[str, Any], top_k: int
) -> tuple[list[dict[str, Any]], bool]:
    """고민 라벨 배열 겹침 → 부족하면 무필터 1회 fallback (01 §2-③ 희소 고민 보완).

    (행 목록, fallback 여부) 를 반환한다. fallback 여부를 호출자에게 알려야 하는 이유는
    그 행들이 고민과 무관하기 때문이다 — 같은 score 를 주면 근거 기반 생성 규칙이
    형해화된다 (`_FALLBACK_SCORE` 주석 참조).
    """
    labels = [label for label in filters.get("concern_labels", []) if label]
    query = client.table("rec_cases").select(
        "case_id, target_concern, skin_concerns, question, answer, cot, "
        "recommended_ingredients, evidence_sources, gender, age"
    )
    if labels:
        query = query.overlaps("skin_concerns", labels)
    # 정렬 없는 limit 은 Postgres 스캔 순서를 그대로 받아 매 요청 다른 행이 나올 수
    # 있다. 벡터 유사도가 없는 지금은 case_id 로라도 결정적 순서를 만든다.
    rows = _narrow(await query.order("case_id").limit(top_k).execute())
    if rows or not labels:
        return rows, False

    # 배열 겹침이 0건인 희소 고민(민감성 6건 등) 보완 — 설계 01 §2-③의 무필터
    # fallback. target_concern 으로 다시 거르면 겹침과 결과가 같아 의미가 없다.
    fallback = _narrow(
        await client.table("rec_cases")
        .select(
            "case_id, target_concern, skin_concerns, question, answer, cot, "
            "recommended_ingredients, evidence_sources, gender, age"
        )
        .order("case_id")
        .limit(top_k)
        .execute()
    )
    return fallback, True


async def _search_efficacy(
    client: Any, filters: dict[str, Any], top_k: int
) -> list[dict[str, Any]]:
    keywords = [kw for kw in filters.get("keywords", []) if kw]
    query = client.table("rec_efficacy").select(
        "id, inci, name_kr, efficacy, safety_note, recommended_concentration, "
        "recommended_skin_types, regulation_note, reference_source, ingredient_id"
    )
    if keywords:
        conditions = ",".join(f"efficacy.ilike.%{kw}%" for kw in keywords)
        query = query.or_(conditions)
    # 정렬 부재 시 id 물리 순서로 앞쪽 행만 반복 회수된다(아스코르빈산류만 나오고
    # 레티놀·아데노신은 어떤 요청에서도 안 나옴). 결정적 순서를 명시한다.
    return _narrow(await query.order("id").limit(top_k).execute())


def _cot_reasoning(cot: Any) -> str:
    """상담의 사고 단계를 근거 텍스트로 편다.

    `cot` 는 jsonb 배열이고 원소가 문자열일 수도 dict 일 수도 있다. dict 를 그대로
    f-string 에 넣으면 `{'step': 1, ...}` repr 이 LLM 프롬프트로 직행한다.
    특정 인덱스만 집던 것도 근거가 없어 전체를 순서대로 잇는다.
    """
    if not isinstance(cot, list):
        return ""
    parts: list[str] = []
    for step in cot:
        if isinstance(step, str):
            parts.append(step)
        elif isinstance(step, dict):
            # 값만 뽑아 잇는다 — 키 이름은 근거가 아니다
            parts.extend(str(v) for v in step.values() if isinstance(v, str | int | float))
    return " ".join(p.strip() for p in parts if str(p).strip())


def _case_chunk(row: dict[str, Any], rank: int, is_fallback: bool = False) -> RetrievedChunk:
    reasoning = _cot_reasoning(row.get("cot"))
    sources = row.get("evidence_sources") or []
    return RetrievedChunk(
        content=f"[상담] {row.get('question', '')}\n[답변] {row.get('answer', '')}\n{reasoning}",
        score=_FALLBACK_SCORE if is_fallback else _rank_score(rank),
        source=ChunkSource(
            doc_id=str(row.get("case_id", "")),
            title=f"{row.get('age') or '연령미상'} {row.get('target_concern', '')} 상담 사례",
            locator=str(sources[0]) if sources else None,
        ),
        metadata={
            "recommended_ingredients": row.get("recommended_ingredients") or [],
            "skin_concerns": row.get("skin_concerns") or [],
            "age": row.get("age"),
            "gender": row.get("gender"),
        },
    )


def _efficacy_chunk(row: dict[str, Any], rank: int) -> RetrievedChunk:
    name = row.get("name_kr") or row.get("inci") or ""
    return RetrievedChunk(
        content=f"[성분] {name}\n[효능] {row.get('efficacy', '')}",
        score=_rank_score(rank),
        source=ChunkSource(
            doc_id=f"eff_{row.get('id')}",
            title=f"{name} 효능",
            locator=row.get("reference_source"),
        ),
        metadata={
            "name_kr": row.get("name_kr"),
            "inci": row.get("inci"),
            "ingredient_id": row.get("ingredient_id"),
            "efficacy": row.get("efficacy"),
            "safety_note": row.get("safety_note"),
            "recommended_concentration": row.get("recommended_concentration"),
            "recommended_skin_types": row.get("recommended_skin_types"),
            "regulation_note": row.get("regulation_note"),  # ⑤ 안전성 필터가 소비 (02 §4)
        },
    )


async def retrieve_for_concerns(
    queries: list[tuple[str, str]],
) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
    """고민별로 두 인덱스를 병렬 검색하고 임계값 미만을 잘라낸다.

    순차 실행하면 검색 지연이 그대로 누적된다. 고민 3개 × 컬렉션 2개 = 6회를
    한꺼번에 띄운다.
    """
    settled = await asyncio.gather(
        *(_retrieve_one(code, query) for code, query in queries), return_exceptions=True
    )
    results = [r for r in settled if not isinstance(r, BaseException)]
    failures = [r for r in settled if isinstance(r, BaseException)]
    if failures:
        logger.warning("검색 %d/%d 건 실패", len(failures), len(settled), exc_info=failures[0])
    # 고민 3개 중 1개만 실패해도 전체를 503 으로 버리면 확보한 근거까지 잃는다.
    # 부분 성공은 살리고, 전부 실패했을 때만 503 으로 올린다.
    if queries and not results:
        raise errors.db_unavailable() from failures[0]

    cases: list[RetrievedChunk] = []
    efficacy: list[RetrievedChunk] = []
    for code, case_chunks, efficacy_chunks in results:
        cases.extend(_above_threshold(case_chunks, code))
        efficacy.extend(_above_threshold(efficacy_chunks, code))
    return cases, efficacy


async def _retrieve_one(
    code: str, query: str
) -> tuple[str, list[RetrievedChunk], list[RetrievedChunk]]:
    """고민 하나에 대해 두 인덱스를 동시에 검색한다."""
    cases, efficacy = await asyncio.gather(
        retrieve(
            RetrievalCollection.REC_CASES,
            query,
            {"concern_labels": [CONCERN_LABEL_BY_CODE[code]]},
            CASES_TOP_K,
        ),
        retrieve(
            RetrievalCollection.REC_EFFICACY,
            query,
            {"keywords": list(CONCERN_SEARCH_KEYWORDS.get(code, ()))},
            EFFICACY_TOP_K,
        ),
    )
    return code, cases, efficacy


def _above_threshold(chunks: list[RetrievedChunk], concern_code: str) -> list[RetrievedChunk]:
    """저score 컷은 retrieve() 가 아니라 호출자인 이 단계가 수행한다."""
    kept = []
    for chunk in chunks:
        if chunk.score >= MIN_RETRIEVAL_SCORE:
            chunk.metadata["concern"] = concern_code
            kept.append(chunk)
    return kept
