"""성분 추천 파이프라인 오케스트레이터.

단계 구현은 `pipeline/` 하위 파일에 있고 여기서는 순서와 조기 종료 조건만 다룬다.

  ① 컨텍스트 조립  `s1_context.py`     — 프로필·BSTI·화장대 (없으면 409)
  ② 질의 구성      `s2_queries.py`     — 고민별 검색어
  ③ 검색           `s3_retrieval.py`   — 두 인덱스 병렬 조회 + 저score 컷
  ④ 후보 집계      `s4_candidates.py`  — 병합·가중치·식약처 ID 연결
  ⑤ 안전성 필터    `s5_safety.py`      — 금지 제거·경고 부착
  ⑥ 생성           `s6_generation.py`  — LLM 호출 1회
  ⑦ BSTI 가지      `s7_bsti.py`       — 타입 권장 성분 + 함유 제품 (⑥과 병렬, LLM 미관여)
  ⑧ 종합 추천      `s8_top.py`         — 고민 축 + BSTI 축 대표 성분 (LLM 미관여)
  ⑨ 제품 추천      `s9_products.py`    — 추천 성분 함유 제품 조회 (LLM 미관여)
  ⑩ 응답 조립      `s10_response.py`    — 배지·출처·제품·확인 불가 응답

설계는 docs/design/01-recommendations-pipeline.md · 04-recommendation-quality.md.
"""

import asyncio
import logging

from app.modules.recommendations.pipeline import (
    s1_context,
    s2_queries,
    s3_retrieval,
    s4_candidates,
    s5_safety,
    s6_generation,
    s7_bsti,
    s8_top,
    s9_products,
    s10_response,
)
from app.modules.recommendations.schemas import (
    Candidate,
    ProductRecommendation,
    RecommendationResponse,
    UserContext,
)

logger = logging.getLogger(__name__)


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
        return s10_response.insufficient(
            user, s10_response.CODE_NO_EVIDENCE, s10_response.NO_EVIDENCE_MESSAGE
        )

    found = await s4_candidates.aggregate_candidates(case_chunks, efficacy_chunks, user)
    safe = await s5_safety.apply_safety_filters(found, user)
    if not safe:
        return s10_response.insufficient(
            user,
            s10_response.CODE_NO_CANDIDATES,
            s10_response.NO_CANDIDATE_MESSAGE,
            action="retry_with_other_concerns",
        )

    # ⑥ 생성(수 초)과 ⑦ BSTI 가지는 서로 의존하지 않는다 — 병렬로 돌려 BSTI 의 DB
    # 왕복을 LLM 대기 시간에 흡수시킨다.
    narrative, (bsti_candidates, bsti_products) = await asyncio.gather(
        s6_generation.generate(user, safe, case_chunks + efficacy_chunks),
        _bsti_branch(user),
    )

    # ⑧ 두 축을 합친 대표 성분 → 그 성분으로 제품을 다시 고른다. 고민 제품 조회와
    # 서로 의존하지 않아 병렬로 묶는다.
    chosen = set(narrative.recommended_names)
    top_picks = s8_top.select_top(safe, chosen, bsti_candidates)
    top_names = {candidate.name_kor for candidate, _ in top_picks}
    products, top_products = await asyncio.gather(
        s9_products.fetch(safe, chosen, user.owned_product_ids),
        # 대표 성분은 두 축에서 오므로 후보도 양쪽을 합쳐 넘긴다(ingredient_id 조인용).
        s9_products.fetch(safe + bsti_candidates, top_names, user.owned_product_ids),
    )
    return s10_response.assemble(
        user,
        safe,
        narrative,
        case_chunks,
        efficacy_chunks,
        products,
        bsti_candidates,
        bsti_products,
        top_picks,
        top_products,
    )


async def _bsti_branch(
    user: UserContext,
) -> tuple[list[Candidate], list[ProductRecommendation]]:
    """⑦ BSTI 가지 — 타입 권장 성분(⑦) → 안전 필터(⑤) → 함유 제품(⑨).

    고민 추천과 독립이라 ⑥과 병렬로 돈다. 부가 정보이므로 어떤 실패든 빈 결과로
    떨어뜨려 핵심인 고민 추천을 깨뜨리지 않는다 — ⑨ 제품 조회와 같은 원칙이다.
    """
    try:
        candidates = await s7_bsti.fetch_bsti_candidates(user)
        if not candidates:
            return [], []
        safe = await s5_safety.apply_safety_filters(candidates, user)
        if not safe:
            return [], []
        products = await s9_products.fetch(
            safe, {c.name_kor for c in safe}, user.owned_product_ids
        )
        return safe, products
    except Exception:
        logger.warning("BSTI 추천 실패 — BSTI 없이 진행", exc_info=True)
        return [], []
