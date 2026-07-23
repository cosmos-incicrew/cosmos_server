"""⑧ 종합 추천 테스트 — 두 축 병합 우선순위·상한·출처 표기.

핵심 계약: 근거가 겹치는 성분(고민 ∩ BSTI)이 맨 위, 그다음 고민(score 순), 그다음
BSTI(표 순서). 고민 축은 ⑥이 실제로 보인 성분만 쓴다 — 설명 없는 추천을 막는다.
"""

from app.modules.recommendations.constants import MAX_TOP_INGREDIENTS
from app.modules.recommendations.pipeline import s8_top, s10_response
from app.modules.recommendations.schemas import Candidate, UserContext


def _cand(name: str, score: float = 0.5) -> Candidate:
    return Candidate(name_kor=name, score=score, efficacy=f"{name} 효능")


def test_overlapping_ingredient_comes_first():
    """양쪽 근거(고민 ∩ BSTI)가 score 가 낮아도 맨 위다 — 근거가 겹치는 게 더 강하다."""
    concern = [_cand("나이아신아마이드", 0.9), _cand("판테놀", 0.6)]
    bsti = [_cand("판테놀", 1.0)]

    picks = s8_top.select_top(concern, {"나이아신아마이드", "판테놀"}, bsti)

    assert [(c.name_kor, src) for c, src in picks] == [
        ("판테놀", s8_top.SOURCE_BOTH),
        ("나이아신아마이드", s8_top.SOURCE_CONCERN),
    ]


def test_concern_beats_bsti_when_no_overlap():
    """겹침이 없으면 고민(지금 겪는 문제)이 타입 권장보다 먼저다."""
    concern = [_cand("나이아신아마이드", 0.8)]
    bsti = [_cand("세라마이드", 1.0)]

    picks = s8_top.select_top(concern, {"나이아신아마이드"}, bsti)

    assert [(c.name_kor, src) for c, src in picks] == [
        ("나이아신아마이드", s8_top.SOURCE_CONCERN),
        ("세라마이드", s8_top.SOURCE_BSTI),
    ]


def test_concern_side_limited_to_names_the_llm_showed():
    """후보에만 있고 ⑥ 서사에 없는 성분은 대표로 올리지 않는다 (설명 없는 추천 방지)."""
    concern = [_cand("추천됨", 0.9), _cand("서사에없음", 0.95)]

    picks = s8_top.select_top(concern, {"추천됨"}, [])

    assert [c.name_kor for c, _ in picks] == ["추천됨"]


def test_concern_side_sorted_by_score():
    concern = [_cand("낮음", 0.4), _cand("높음", 0.9), _cand("중간", 0.6)]

    picks = s8_top.select_top(concern, {"낮음", "높음", "중간"}, [])

    assert [c.name_kor for c, _ in picks] == ["높음", "중간", "낮음"]


def test_bsti_side_keeps_table_order():
    """BSTI 는 표 순서가 곧 대표성이라 score 로 재정렬하지 않는다."""
    bsti = [_cand("첫째", 1.0), _cand("둘째", 1.0), _cand("셋째", 1.0)]

    picks = s8_top.select_top([], set(), bsti)

    assert [c.name_kor for c, _ in picks] == ["첫째", "둘째", "셋째"]


def test_caps_at_max_and_dedupes():
    """상한에서 자르고, 같은 성분이 두 축에 있어도 한 번만 싣는다."""
    concern = [_cand(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_TOP_INGREDIENTS + 3)]
    bsti = [_cand("성분0"), _cand("성분1")]  # 이미 고민 축에 있는 것들

    picks = s8_top.select_top(concern, {c.name_kor for c in concern}, bsti)

    assert len(picks) == MAX_TOP_INGREDIENTS
    assert len({c.name_kor for c, _ in picks}) == MAX_TOP_INGREDIENTS


def test_empty_both_axes_yields_nothing():
    assert s8_top.select_top([], set(), []) == []


def test_card_carries_match_source():
    """⑩ 이 종합 카드에 출처를 실어 프론트가 근거를 구분할 수 있게 한다."""
    picks = [(_cand("판테놀"), s8_top.SOURCE_BOTH), (_cand("세라마이드"), s8_top.SOURCE_BSTI)]

    cards = s10_response._top_ingredients(picks, UserContext(user_id="u"))

    assert [(c.name_kor, c.match_source) for c in cards] == [
        ("판테놀", "both"),
        ("세라마이드", "bsti"),
    ]


def test_other_lists_have_no_match_source():
    """고민·BSTI 목록은 축이 이미 정해져 있어 출처를 달지 않는다 (종합에서만 쓴다)."""
    cards = s10_response._bsti_ingredients([_cand("세라마이드")], UserContext(user_id="u"))

    assert cards[0].match_source is None
