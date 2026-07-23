"""③ Retrieval — "믿을 수 있는 자료를 찾아온다".

고민별 질의로 rec_cases · rec_efficacy 두 인덱스를 병렬 검색한다 (설계 01 §2-③).

성분 해설(이호영)은 자체 조회를 쓰므로 이 모듈은 추천 전용이다 (공통 유틸 아님).
이 함수는 **자료를 찾아오는 데까지만** 하고 생성·프롬프트·저score 컷은 호출자 몫이다.

  - score 는 0~1 정규화
  - 저score 컷은 여기서 하지 않는다 — 부르는 쪽(service)이 판단
  - 결과 없으면 예외가 아니라 빈 목록

**벡터 검색.** 질의를 gemini-embedding-001 로 임베딩(embedding.py)해 Postgres RPC
(`match_rec_cases`/`match_rec_efficacy`, HNSW 코사인, 설계 03)로 top_k 를 가져온다.
score 는 RPC 가 계산한 코사인 유사도를 그대로 쓴다.
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
from app.modules.recommendations.embedding import embed_query, to_pgvector
from app.modules.recommendations.schemas import ChunkSource, RetrievedChunk

logger = logging.getLogger(__name__)


class RetrievalCollection(StrEnum):
    """검색 대상 인덱스. 문자열 오타를 막으려고 Enum 으로 고정한다."""

    REC_CASES = "rec_cases"
    REC_EFFICACY = "rec_efficacy"


class RetrievalError(RuntimeError):
    """검색 실패. 호출자는 이 예외 하나만 잡으면 된다."""


async def retrieve(
    collection: RetrievalCollection,
    query_text: str,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """질의 텍스트를 임베딩해 벡터 검색 RPC 로 top_k 청크를 가져온다.

    score 는 RPC 가 준 코사인 유사도(0~1). 결과 없으면 빈 목록, 실패는 RetrievalError.
    """
    try:
        vector = to_pgvector(await embed_query(query_text))
        client = await get_supabase()
        fn = (
            "match_rec_cases"
            if collection is RetrievalCollection.REC_CASES
            else "match_rec_efficacy"
        )
        rows = _narrow(
            await client.rpc(fn, {"query_embedding": vector, "match_count": top_k}).execute()
        )
        if collection is RetrievalCollection.REC_CASES:
            return [_case_chunk(row) for row in rows]
        return [_efficacy_chunk(row) for row in rows]
    except Exception as exc:  # 실패는 종류를 가리지 않고 하나의 예외로 감싼다
        raise RetrievalError(f"{collection.value} 검색 실패: {exc}") from exc


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


def _case_chunk(row: dict[str, Any]) -> RetrievedChunk:
    reasoning = _cot_reasoning(row.get("cot"))
    sources = row.get("evidence_sources") or []
    return RetrievedChunk(
        content=f"[상담] {row.get('question', '')}\n[답변] {row.get('answer', '')}\n{reasoning}",
        score=float(row.get("score") or 0.0),
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
            "target_concern": row.get("target_concern", ""),
            "skin_type": row.get("skin_type"),
            "question": row.get("question", ""),
            "answer": row.get("answer", ""),
        },
    )


def _efficacy_chunk(row: dict[str, Any]) -> RetrievedChunk:
    name = row.get("name_kor") or row.get("inci") or ""
    return RetrievedChunk(
        content=f"[성분] {name}\n[효능] {row.get('efficacy', '')}",
        score=float(row.get("score") or 0.0),
        source=ChunkSource(
            doc_id=f"eff_{row.get('id')}",
            title=f"{name} 효능",
            locator=row.get("reference_source"),
        ),
        metadata={
            "name_kor": row.get("name_kor"),
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
    """고민 하나에 대해 두 leg 를 동시에 벡터 검색한다.

    cases leg 는 사람묘사+고민 질의(query), efficacy leg 는 고민 구절
    (CONCERN_SEARCH_KEYWORDS) 로 검색한다 — 효능 코퍼스엔 짧은 구절이 맞는다(설계 03 §4).
    """
    efficacy_query = " ".join(CONCERN_SEARCH_KEYWORDS.get(code, ())) or CONCERN_LABEL_BY_CODE[code]
    # 두 leg 는 독립이다 — 한 leg 가 실패(embed_query 장애·RPC 실패)해도 성공한 leg 의
    # 근거는 살린다. return_exceptions 없이 gather 하면 한 leg 의 예외가 성공한 leg 결과
    # 까지 버린다. 두 leg 가 모두 실패했을 때만 이 고민을 실패로 올려, retrieve_for_concerns
    # 의 "전부 실패 시에만 503" 판정에 맡긴다(부분 성공은 살린다).
    cases_r, efficacy_r = await asyncio.gather(
        retrieve(RetrievalCollection.REC_CASES, query, CASES_TOP_K),
        retrieve(RetrievalCollection.REC_EFFICACY, efficacy_query, EFFICACY_TOP_K),
        return_exceptions=True,
    )
    if isinstance(cases_r, BaseException) and isinstance(efficacy_r, BaseException):
        raise RetrievalError(f"{code} 고민 양 leg 검색 실패: {cases_r}") from cases_r

    cases: list[RetrievedChunk]
    if isinstance(cases_r, BaseException):
        logger.warning("%s 고민 cases leg 실패, efficacy leg 만 사용", code, exc_info=cases_r)
        cases = []
    else:
        cases = cases_r

    efficacy: list[RetrievedChunk]
    if isinstance(efficacy_r, BaseException):
        logger.warning("%s 고민 efficacy leg 실패, cases leg 만 사용", code, exc_info=efficacy_r)
        efficacy = []
    else:
        efficacy = efficacy_r

    return code, cases, efficacy


def _above_threshold(chunks: list[RetrievedChunk], concern_code: str) -> list[RetrievedChunk]:
    """저score 컷은 retrieve() 가 아니라 호출자인 이 단계가 수행한다."""
    kept = []
    for chunk in chunks:
        if chunk.score >= MIN_RETRIEVAL_SCORE:
            chunk.metadata["concern"] = concern_code
            kept.append(chunk)
    return kept
