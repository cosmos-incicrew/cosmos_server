"""성분 추천 파이프라인 오케스트레이터.

단계 구현은 `pipeline/` 하위 파일에 있고 여기서는 순서와 조기 종료 조건만 다룬다.

  ① 컨텍스트 조립  `s1_context.py`     — 프로필·BSTI·화장대 (없으면 409)
  ② 질의 구성      `s2_queries.py`     — 고민별 검색어
  ③ 검색           `s3_retrieval.py`   — 두 인덱스 병렬 조회 + 저score 컷
  ④ 후보 집계      `s4_candidates.py`  — 병합·가중치·식약처 ID 연결
  ⑤ 안전성 필터    `s5_safety.py`      — 금지 제거·경고 부착
  ⑥ 생성           `s6_generation.py`  — LLM 호출 1회
  ⑦ 응답 조립      `s7_response.py`    — 배지·출처·확인 불가 응답

설계는 docs/design/01-recommendations-pipeline.md.
"""

from app.modules.recommendations.pipeline import (
    s1_context,
    s2_queries,
    s3_retrieval,
    s4_candidates,
    s5_safety,
    s6_generation,
    s7_response,
)
from app.modules.recommendations.schemas import RecommendationResponse


async def create_recommendations(user_id: str) -> RecommendationResponse:
    """엔드포인트 진입점.

    조기 종료가 두 군데 있다 — 둘 다 LLM 을 부르지 않고 "확인 불가" 정형 응답을
    돌려준다. 근거 없이 생성하지 않는다는 규칙이자 불필요한 과금 차단이다
    (docs/rules/llm-rag-rules.md).
    """
    user = await s1_context.build_context(user_id)

    search_queries = s2_queries.build_queries(user)
    case_chunks, efficacy_chunks = await s3_retrieval.retrieve_for_concerns(search_queries)
    if not case_chunks and not efficacy_chunks:
        return s7_response.insufficient(user, s7_response.NO_EVIDENCE_MESSAGE)

    found = await s4_candidates.aggregate_candidates(case_chunks, efficacy_chunks, user)
    safe = await s5_safety.apply_safety_filters(found, user)
    if not safe:
        return s7_response.insufficient(
            user, s7_response.NO_CANDIDATE_MESSAGE, action="retry_with_other_concerns"
        )

    chunks = case_chunks + efficacy_chunks
    picks = await s6_generation.generate(user, safe, chunks)
    return s7_response.assemble(user, safe, picks, chunks)
