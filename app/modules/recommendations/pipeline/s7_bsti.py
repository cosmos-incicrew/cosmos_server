"""⑦ BSTI 권장 성분 — "타입에 맞는 성분을 확정 표에서 가져온다".

고민 기반 추천(③④)과 **별개 축**이다. 고민은 "지금 겪는 문제", BSTI 는 "타입상 늘
맞는 성분"이라 근거가 다르므로 응답에도 따로 싣는다(`bsti_ingredients`). 두 축에 같은
성분이 겹쳐 나올 수 있는데, 겹침은 숨기지 않는다 — 근거가 다르기 때문이다.

검색이 아니라 확정 표(`bsti_ingredients.BSTI_RECOMMENDED`)라 벡터·LLM 이 관여하지
않는다. 효능·주의 정보를 붙이려고 `rec_efficacy` 에서 이름으로 조회할 뿐이며, 이후
⑤ 안전 필터·⑨ 제품 조회는 고민 추천과 같은 구현을 그대로 재사용한다.
"""

import logging

from app.core.supabase import get_supabase, rows
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.recommendations.schemas import Candidate, UserContext

logger = logging.getLogger(__name__)

# BSTI 성분엔 검색 유사도가 없다 — 표에 있으면 타입 확정 매칭이라 1.0 으로 통일한다
# (⑩ 이 IngredientEvidence.similarity 로 그대로 쓴다).
_BSTI_SCORE = 1.0

_EFFICACY_COLUMNS = (
    "ingredient_id, inci, name_kor, efficacy, safety_note, "
    "recommended_concentration, recommended_skin_types, regulation_note"
)


async def fetch_bsti_candidates(context: UserContext) -> list[Candidate]:
    """BSTI 권장 성분을 효능 사전 근거와 함께 후보로 만든다.

    검사 전(`bsti_type=None`)이면 빈 목록이다. 조회에 실패해도 예외를 올리지 않고 빈
    목록을 돌려준다 — BSTI 추천은 부가 정보라 고민 추천을 깨뜨리면 안 된다(⑨과 같은 원칙).

    순서는 `BSTI_RECOMMENDED` 표 순서를 그대로 둔다. 앞쪽이 그 타입의 대표 성분이라
    점수로 재정렬하지 않는다.

    한계: `rec_efficacy.name_kor` 과 정확히 일치하는 성분만 싣는다. 원본에 개행·괄호가
    섞인 이름(2,270행 중 27건)은 매칭되지 않아 빠질 수 있다 — 실데이터 QA 로 확인한다.
    """
    names = context.bsti_recommended
    if not names:
        return []

    try:
        client = await get_supabase()
        eff_rows = rows(
            await client.table("rec_efficacy")
            .select(_EFFICACY_COLUMNS)
            .in_("name_kor", names)
            .execute()
        )
    except Exception:
        logger.warning("BSTI 권장 성분 조회 실패 — BSTI 추천 없이 진행", exc_info=True)
        return []

    by_name = {str(row["name_kor"]): row for row in eff_rows if row.get("name_kor")}
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for name in names:
        row = by_name.get(name)
        if row is None:
            continue  # 효능 사전에 없는 표 이름 — 근거 없이 추천하지 않는다
        key = normalize_ingredient_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(
            Candidate(
                name_kor=key,
                score=_BSTI_SCORE,
                ingredient_id=row.get("ingredient_id"),
                inci=row.get("inci"),
                efficacy=row.get("efficacy"),
                safety_note=row.get("safety_note"),
                recommended_concentration=row.get("recommended_concentration"),
                recommended_skin_types=row.get("recommended_skin_types"),
                regulation_note=row.get("regulation_note"),
            )
        )
    return candidates
