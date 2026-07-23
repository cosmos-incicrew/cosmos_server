"""성분 추천 파이프라인 오케스트레이터.

단계 구현은 `pipeline/` 하위 파일에 있고 여기서는 순서와 조기 종료 조건만 다룬다.

  ① 컨텍스트 조립  `s1_context.py`     — 프로필·BSTI·화장대 (없으면 409)
  ② 질의 구성      `s2_queries.py`     — 고민별 검색어
  ③ 검색           `s3_retrieval.py`   — 두 인덱스 병렬 조회 + 저score 컷
  ④ 후보 집계      `s4_candidates.py`  — 병합·가중치·식약처 ID 연결
  ⑤ 안전성 필터    `s5_safety.py`      — 금지 제거·경고 부착
  ⑥ 생성           `s6_generation.py`  — LLM 호출 1회
  ⑦ BSTI 가지      `s7_bsti.py`        — 타입 권장 성분 (③과 병렬, 추천 생성엔 미관여)
  ⑧ 종합 추천      `s8_top_picks.py`   — 고민 축 + BSTI 축 대표 성분 (LLM 미관여)
  ⑨ 제품 추천      `s9_products.py`    — 대표 성분 함유 제품 조회 (LLM 미관여)
  ⑩ 응답 조립      `s10_response.py`   — 배지·출처·제품·확인 불가 응답

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
    s8_top_picks,
    s9_products,
    s10_response,
)
from app.modules.recommendations.schemas import (
    Candidate,
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
    return await run_for_context(await s1_context.build_context(user_id))


async def run_for_context(user: UserContext) -> RecommendationResponse:
    """②~⑩ — ① 프로필 조회를 뺀 본체.

    분리해 둔 이유는 유스케이스 검증이다(`scripts/usecase_recommendations.py`). 임의의
    나이·성별·BSTI·고민 조합을 실제 파이프라인에 태우려면 컨텍스트를 주입할 자리가
    있어야 하는데, 그 조합마다 `user_profiles` 행을 쓰는 건 검증을 위해 운영 데이터를
    건드리는 일이다.
    """
    search_queries = s2_queries.build_queries(user)
    # ⑦ BSTI 가지는 ① 컨텍스트에만 의존해 ③④⑤ 어디에도 매이지 않는다. ⑥ 프롬프트가
    # BSTI 축 성분의 영어 원문까지 번역시키려면 ⑥ 시작 시점에 이미 있어야 해서
    # 앞당겼고, ③ 검색과 병렬로 돌려 DB 왕복을 검색 대기 시간에 흡수시킨다.
    (case_chunks, efficacy_chunks), bsti_candidates = await asyncio.gather(
        s3_retrieval.retrieve_for_concerns(search_queries),
        _bsti_branch(user),
    )
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

    # BSTI 후보를 함께 넘기는 것은 추천 대상이 아니라 번역 대상으로서다 (s6 docstring).
    narrative = await s6_generation.generate(
        user, safe, case_chunks + efficacy_chunks, bsti_candidates
    )

    # ⑧ 두 축을 합친 대표 성분 → 그 성분으로만 제품을 조회한다. 축별 제품 목록이
    # 응답에서 빠지면서 ⑨ 왕복이 3회에서 1회로 줄었다 (2026-07-23).
    top_picks = s8_top_picks.select_top(safe, set(narrative.recommended_names), bsti_candidates)
    top_products = await s9_products.fetch(
        # 대표 성분은 두 축에서 오므로 후보도 양쪽을 합쳐 넘긴다(ingredient_id 조인용).
        safe + bsti_candidates,
        {candidate.name_kor for candidate, _ in top_picks},
        user.owned_product_ids,
    )
    return s10_response.assemble(
        user, narrative, case_chunks, efficacy_chunks, top_picks, top_products
    )


async def _bsti_branch(user: UserContext) -> list[Candidate]:
    """⑦ BSTI 가지 — 타입 권장 성분(⑦) → 안전 필터(⑤). 산출은 ⑧ 종합으로 합류한다.

    고민 추천과 독립이라 ③ 검색과 병렬로 돈다. 부가 정보이므로 어떤 실패든 빈 결과로
    떨어뜨려 핵심인 고민 추천을 깨뜨리지 않는다 — ⑨ 제품 조회와 같은 원칙이다.

    ⑤를 고민 축과 한 번에 묶지 않는 이유는 이 병렬 배치 때문이다. 고민 축 ⑤는 ③④를
    기다려야 해서, 합치면 BSTI 조회가 검색 뒤로 직렬화된다 — 왕복 1회를 아끼려고
    지연을 늘리는 맞바꿈이 된다.
    """
    try:
        candidates = await s7_bsti.fetch_bsti_candidates(user)
        if not candidates:
            return []
        return await s5_safety.apply_safety_filters(candidates, user)
    except Exception:
        logger.warning("BSTI 추천 실패 — BSTI 없이 진행", exc_info=True)
        return []
