"""⑦ BSTI 권장 성분 — "타입에 맞는 성분을 확정 표에서 가져온다".

고민 기반 추천(③④)과 **별개 축**이다. 고민은 "지금 겪는 문제", BSTI 는 "타입상 늘
맞는 성분"이라 근거가 다르다. 응답에 축별 배열을 따로 싣던 방식은 걷었고(2026-07-23),
지금은 ⑧ 종합에서 고민 축과 합쳐지며 출처를 `match_source="bsti"` 로 남긴다 — 축 구분은
사라진 게 아니라 배열에서 필드로 옮겨간 것이다. 두 축에 같은 성분이 겹치면 ⑧이 `both`
로 올린다.

검색이 아니라 확정 표(`bsti_ingredients.BSTI_RECOMMENDED`)라 벡터·LLM 이 관여하지
않는다. 효능·주의 정보를 붙이려고 `rec_efficacy` 에서 이름으로 조회할 뿐이며, 이후
⑤ 안전 필터는 고민 추천과 같은 구현을 그대로 재사용한다.
"""

import logging
from typing import Any

from app.core.supabase import get_supabase, rows
from app.modules.recommendations import bsti_ingredients
from app.modules.recommendations.constants import EFFICACY_FIELDS
from app.modules.recommendations.schemas import Candidate, UserContext
from app.modules.recommendations.util.ingredient_names import normalize_ingredient_name

logger = logging.getLogger(__name__)

# BSTI 성분엔 검색 유사도가 없다 — 내부 순위·정렬용으로만 1.0 을 준다. 사용자 표시는
# ⑩ 이 BSTI 축 similarity 를 null 로 비운다 (검색값이 아니라서 — schemas 주석 참조).
_BSTI_SCORE = 1.0

# 이름으로 조회하므로 name_kor 이 추가로 필요하다 (③④가 쓰는 필드 + 조회 키).
_EFFICACY_COLUMNS = ", ".join(("name_kor", *EFFICACY_FIELDS))


def _first_usable(
    group: tuple[str, ...], by_name: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """표 항목의 별칭 중 쓸 수 있는 첫 행. 식약처 연결분을 우선한다.

    별칭 순서는 대표성 순이라 그대로 따르되, 앞쪽이 미연결이면 뒤쪽 연결분을 쓴다 —
    항목 하나가 통째로 빠지는 것보다 낫다.
    """
    fallback: tuple[str, dict[str, Any] | None] = ("", None)
    for name in group:
        row = by_name.get(name)
        if row is None:
            continue
        if row.get("ingredient_id") is not None:
            return name, row
        if fallback[1] is None:
            fallback = (name, row)
    return fallback


async def fetch_bsti_candidates(context: UserContext) -> list[Candidate]:
    """BSTI 권장 성분을 효능 사전 근거와 함께 후보로 만든다.

    검사 전(`bsti_type=None`)이면 빈 목록이다. 조회에 실패해도 예외를 올리지 않고 빈
    목록을 돌려준다 — BSTI 추천은 부가 정보라 고민 추천을 깨뜨리면 안 된다(⑨과 같은 원칙).

    순서는 `BSTI_RECOMMENDED` 표 순서를 그대로 둔다. 앞쪽이 그 타입의 대표 성분이라
    점수로 재정렬하지 않는다.

    식약처 원료(`ingredient_id`)에 연결된 성분만 싣는다 — 아래 제외 주석 참조.

    한계: `rec_efficacy.name_kor` 과 정확히 일치하는 성분만 싣는다. 원본에 개행·괄호가
    섞인 이름(2,270행 중 27건)은 매칭되지 않아 빠질 수 있다 — 실데이터 QA 로 확인한다.
    """
    names = context.bsti_recommended
    if not names:
        return []
    # 표 항목 단위로 되묶는다 — 평평한 채로 쓰면 `히알루론산` 한 항목의 별칭 3개가
    # 대표 카드를 통째로 먹는다(실측). 항목당 하나만 고른다.
    groups = bsti_ingredients.group_by_table_entry(names)

    try:
        client = await get_supabase()
        eff_rows = rows(
            await client.table("rec_efficacy")
            .select(_EFFICACY_COLUMNS)
            .in_("name_kor", names)
            .execute()
        )
    except Exception:
        # 호출부(`service._bsti_branch`)도 가지 전체를 감싸지만 그건 ⑤·⑨까지 덮는 바깥
        # 방어다. 여기 것은 "조회 실패"를 그 사유로 로그에 남기려고 따로 잡는다 —
        # 지우면 실패 원인이 뭉뚱그려진 바깥 로그 하나로만 남는다.
        logger.warning("BSTI 권장 성분 조회 실패 — BSTI 추천 없이 진행", exc_info=True)
        return []

    by_name = {str(row["name_kor"]): row for row in eff_rows if row.get("name_kor")}
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for group in groups:
        name, row = _first_usable(group, by_name)
        if row is None:
            continue  # 효능 사전에 없거나 전부 미연결인 표 항목 — 근거 없이 추천하지 않는다
        if row.get("ingredient_id") is None:
            # 식약처 미연결 성분은 두 곳이 동시에 깨진다: ⑨ 제품 조회가 ingredient_id 로
            # 역조인해 제품이 **구조적으로** 0건이고, ⑤가 `안전성확인불가` 경고를 붙인다.
            # 원인이 하나(식약처 미연결)라 검사도 하나로 둔다. ⑧이 BSTI 에 대표 카드
            # 2칸을 조건 없이 내주므로 여기서 거르지 않으면 "제품도 없고 안전성도 모르는
            # 성분"이 메인 카드를 차지한다. 실측(2026-07-23) 16개 타입 모두 제외 후
            # 6개 이상 남아 폴백은 두지 않는다 — 남길 근거가 곧 이 버그다.
            logger.info("식약처 미연결 BSTI 권장 성분 제외: %s", name)
            continue
        key = normalize_ingredient_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(
            Candidate(
                name_kor=key,
                score=_BSTI_SCORE,
                **{field: row.get(field) for field in EFFICACY_FIELDS},
            )
        )
    return candidates
