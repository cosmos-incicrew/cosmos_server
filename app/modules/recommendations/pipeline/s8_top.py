"""⑧ 종합 추천 — 고민 축과 BSTI 축을 합쳐 대표 성분을 고른다.

두 축은 근거가 달라 응답에는 따로 싣지만(`ingredients` vs `bsti_ingredients`), 사용자가
결국 보는 건 "그래서 뭘 쓰면 되나"다. **근거가 겹칠수록 위로** 올려 최대
`MAX_TOP_INGREDIENTS` 개를 고른다. 설계 04 §8.

LLM 은 관여하지 않는다 — 두 축의 산출을 순위 규칙으로 합칠 뿐이라 생성이 개입할 여지가
없다. 제품은 이 대표 성분으로 ⑨ 커버리지 그리디를 그대로 재사용한다.
"""

from app.modules.recommendations.constants import MAX_TOP_INGREDIENTS
from app.modules.recommendations.schemas import Candidate

# match_source — 프론트가 근거를 구분해 배지로 보여준다.
SOURCE_BOTH = "both"  # 고민에도 좋고 타입에도 맞음
SOURCE_CONCERN = "concern"  # 지금 겪는 고민 기반
SOURCE_BSTI = "bsti"  # BSTI 타입 권장


def select_top(
    concern_candidates: list[Candidate],
    recommended_names: set[str],
    bsti_candidates: list[Candidate],
) -> list[tuple[Candidate, str]]:
    """(성분, 출처) 목록을 근거가 겹치는 순으로 돌려준다.

    1. 고민 추천 ∩ BSTI 권장 — 양쪽 근거라 가장 강하다
    2. 고민 추천 (score 순) — "지금 겪는 문제"가 타입 적합보다 급하다
    3. BSTI 권장 (표 순서) — 타입 대표 성분

    고민 축은 `recommended_names`(⑥이 사용자에게 실제로 보인 성분)로 한정한다. 후보에만
    있고 서사에 없는 성분을 대표로 올리면 설명 없는 추천이 된다.
    """
    bsti_names = {c.name_kor for c in bsti_candidates}
    concern = sorted(
        (c for c in concern_candidates if c.name_kor in recommended_names),
        key=lambda c: c.score,
        reverse=True,
    )

    picks: list[tuple[Candidate, str]] = []
    seen: set[str] = set()

    def _add(candidate: Candidate, source: str) -> None:
        if candidate.name_kor in seen:
            return
        seen.add(candidate.name_kor)
        picks.append((candidate, source))

    for candidate in concern:
        if candidate.name_kor in bsti_names:
            _add(candidate, SOURCE_BOTH)
    for candidate in concern:
        _add(candidate, SOURCE_CONCERN)
    for candidate in bsti_candidates:
        _add(candidate, SOURCE_BSTI)

    return picks[:MAX_TOP_INGREDIENTS]
