"""⑧ 종합 추천 — 고민 축과 BSTI 축을 합쳐 대표 성분을 고른다.

두 축은 근거가 다르지만 사용자가 결국 보는 건 "그래서 뭘 쓰면 되나"다. **근거가 겹칠수록
위로** 올려 최대 `MAX_TOP_INGREDIENTS` 개를 고른다. 설계 04 §8.

축은 배열이 아니라 `match_source` 로 구분해 내보낸다 — 응답이 이 종합 목록 하나만
싣기 때문이다(2026-07-23). ⑩ 이 제품 출처도 여기 산출을 되짚어 붙인다.

LLM 은 관여하지 않는다 — 두 축의 산출을 순위 규칙으로 합칠 뿐이라 생성이 개입할 여지가
없다. 제품은 이 대표 성분으로 ⑨ 커버리지 그리디를 그대로 재사용한다.
"""

from app.modules.recommendations.constants import (
    MAX_TOP_CONCERN_INGREDIENTS,
    MAX_TOP_INGREDIENTS,
)
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

    우선순위는 위 그대로지만 고민 축이 전량을 가져가지는 못한다 — `MAX_TOP_CONCERN_INGREDIENTS`
    까지만 싣고 나머지는 BSTI 몫으로 비워 둔다. 상한 없이 순위만 적용하면 "고민+BSTI 종합"이
    실제로는 고민 목록이 된다(constants ⑧절).
    """
    bsti_keys = {_identity(c) for c in bsti_candidates}
    concern = sorted(
        (c for c in concern_candidates if c.name_kor in recommended_names),
        key=lambda c: c.score,
        reverse=True,
    )

    picks: list[tuple[Candidate, str]] = []
    seen: set[object] = set()

    def _add(candidate: Candidate, source: str) -> None:
        key = _identity(candidate)
        if key in seen:
            return
        seen.add(key)
        picks.append((candidate, source))

    for candidate in concern:
        if _identity(candidate) in bsti_keys:
            _add(candidate, SOURCE_BOTH)
    for candidate in concern:
        _add(candidate, SOURCE_CONCERN)

    # 고민 축 상한은 BSTI 유무로 갈린다 — BSTI 후보가 있으면 그 몫(2칸)을 남기려 3칸으로
    # 자르고, 없으면 비울 몫이 없으니 MAX_TOP_INGREDIENTS 까지 고민이 다 쓴다. 자른 뒤
    # `seen` 을 다시 세운다 — 상한에 밀린 성분이 BSTI 표에도 있다면 타입 근거로 다시
    # 들어올 수 있어야 한다.
    concern_limit = MAX_TOP_CONCERN_INGREDIENTS if bsti_candidates else MAX_TOP_INGREDIENTS
    picks = picks[:concern_limit]
    seen = {_identity(candidate) for candidate, _ in picks}

    for candidate in bsti_candidates:
        _add(candidate, SOURCE_BSTI)

    return picks[:MAX_TOP_INGREDIENTS]


def _identity(candidate: Candidate) -> object:
    """같은 성분인지 판정하는 키 (④ `_dedupe_resolved` 와 같은 규칙).

    이름으로 보면 두 축이 표시명만 다른 같은 성분(`히알루론산`·`하이알루로닉애씨드`,
    둘 다 id=77)을 서로 다른 성분으로 취급해 메인 카드 5칸 중 2칸을 먹고, 붙어야 할
    `both` 배지도 안 붙는다. `or` 를 쓰면 ingredient_id 가 0 인 성분이 이름 키로 샌다.
    """
    return candidate.name_kor if candidate.ingredient_id is None else candidate.ingredient_id
