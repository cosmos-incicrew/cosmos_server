"""⑧ 종합 추천 테스트 — 두 축 병합 우선순위·상한·출처 표기.

핵심 계약: 근거가 겹치는 성분(고민 ∩ BSTI)이 맨 위, 그다음 고민(score 순), 그다음
BSTI(표 순서). 고민 축은 ⑥이 실제로 보인 성분만 쓴다 — 설명 없는 추천을 막는다.
"""

from app.modules.recommendations.constants import (
    MAX_TOP_CONCERN_INGREDIENTS,
    MAX_TOP_INGREDIENTS,
)
from app.modules.recommendations.pipeline import s8_top_picks, s10_response
from app.modules.recommendations.schemas import Candidate, UserContext


def _cand(name: str, score: float = 0.5, ingredient_id: int | None = None) -> Candidate:
    return Candidate(
        name_kor=name, score=score, efficacy=f"{name} 효능", ingredient_id=ingredient_id
    )


def test_overlapping_ingredient_comes_first():
    """양쪽 근거(고민 ∩ BSTI)가 score 가 낮아도 맨 위다 — 근거가 겹치는 게 더 강하다."""
    concern = [_cand("나이아신아마이드", 0.9), _cand("판테놀", 0.6)]
    bsti = [_cand("판테놀", 1.0)]

    picks = s8_top_picks.select_top(concern, {"나이아신아마이드", "판테놀"}, bsti)

    assert [(c.name_kor, src) for c, src in picks] == [
        ("판테놀", s8_top_picks.SOURCE_BOTH),
        ("나이아신아마이드", s8_top_picks.SOURCE_CONCERN),
    ]


def test_concern_beats_bsti_when_no_overlap():
    """겹침이 없으면 고민(지금 겪는 문제)이 타입 권장보다 먼저다."""
    concern = [_cand("나이아신아마이드", 0.8)]
    bsti = [_cand("세라마이드", 1.0)]

    picks = s8_top_picks.select_top(concern, {"나이아신아마이드"}, bsti)

    assert [(c.name_kor, src) for c, src in picks] == [
        ("나이아신아마이드", s8_top_picks.SOURCE_CONCERN),
        ("세라마이드", s8_top_picks.SOURCE_BSTI),
    ]


def test_concern_side_limited_to_names_the_llm_showed():
    """후보에만 있고 ⑥ 서사에 없는 성분은 대표로 올리지 않는다 (설명 없는 추천 방지)."""
    concern = [_cand("추천됨", 0.9), _cand("서사에없음", 0.95)]

    picks = s8_top_picks.select_top(concern, {"추천됨"}, [])

    assert [c.name_kor for c, _ in picks] == ["추천됨"]


def test_concern_side_sorted_by_score():
    concern = [_cand("낮음", 0.4), _cand("높음", 0.9), _cand("중간", 0.6)]

    picks = s8_top_picks.select_top(concern, {"낮음", "높음", "중간"}, [])

    assert [c.name_kor for c, _ in picks] == ["높음", "중간", "낮음"]


def test_bsti_side_keeps_table_order():
    """BSTI 는 표 순서가 곧 대표성이라 score 로 재정렬하지 않는다."""
    bsti = [_cand("첫째", 1.0), _cand("둘째", 1.0), _cand("셋째", 1.0)]

    picks = s8_top_picks.select_top([], set(), bsti)

    assert [c.name_kor for c, _ in picks] == ["첫째", "둘째", "셋째"]


def test_caps_at_max_and_dedupes():
    """상한에서 자르고, 같은 성분이 두 축에 있어도 한 번만 싣는다."""
    concern = [_cand(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_TOP_INGREDIENTS + 3)]
    # 앞 둘은 이미 고민 축에 있는 것들(중복 확인), 뒤는 BSTI 전용
    bsti = [_cand("성분0"), _cand("성분1"), _cand("타입1"), _cand("타입2")]

    picks = s8_top_picks.select_top(concern, {c.name_kor for c in concern}, bsti)

    assert len(picks) == MAX_TOP_INGREDIENTS
    assert len({c.name_kor for c, _ in picks}) == MAX_TOP_INGREDIENTS


def test_bsti_keeps_its_slots_when_concern_axis_is_full():
    """고민 축이 종합 카드를 다 먹으면 안 된다 — BSTI 몫이 남아야 "종합"이다.

    고민 상한이 없으면 ⑥이 상한껏 권할 때 BSTI 칸이 산술적으로 0이 되고, 실 데모가
    정확히 그 상태였다(5칸 전부 고민, `[피부타입]` 0개).
    """
    concern = [_cand(f"고민{i}", 0.9 - i * 0.01) for i in range(MAX_TOP_INGREDIENTS)]
    bsti = [_cand("타입1"), _cand("타입2")]

    picks = s8_top_picks.select_top(concern, {c.name_kor for c in concern}, bsti)

    sources = [src for _, src in picks]
    assert sources.count(s8_top_picks.SOURCE_CONCERN) == MAX_TOP_CONCERN_INGREDIENTS
    assert [c.name_kor for c, src in picks if src == s8_top_picks.SOURCE_BSTI] == ["타입1", "타입2"]


def test_concern_overflow_returns_through_the_bsti_axis():
    """고민 상한에 밀린 성분도 BSTI 표에 있으면 타입 근거로 다시 들어온다.

    안 그러면 BSTI 칸을 비워 둔 채 그 성분이 통째로 사라진다.
    """
    # 양쪽 근거(both)가 고민 상한보다 많은 경우 — 상한에 밀리는 건 점수가 가장 낮은 것
    concern = [_cand(f"성분{i}", 0.9 - i * 0.01) for i in range(MAX_TOP_CONCERN_INGREDIENTS + 1)]
    overflow = concern[-1]

    picks = s8_top_picks.select_top(concern, {c.name_kor for c in concern}, concern)

    assert (overflow.name_kor, s8_top_picks.SOURCE_BSTI) in [(c.name_kor, src) for c, src in picks]


def test_same_ingredient_with_different_display_names_merges_as_both():
    """표시명만 다른 같은 성분(id 동일)이 메인 카드 두 칸을 먹으면 안 된다.

    이름으로만 보면 `히알루론산`(고민 축)과 `하이알루로닉애씨드`(BSTI 축)가 다른 성분이
    되어 5칸 중 2칸을 같은 성분이 차지하고, 붙어야 할 `both` 배지도 안 붙는다.
    """
    concern = [_cand("히알루론산", 0.9, ingredient_id=77)]
    bsti = [_cand("하이알루로닉애씨드", 1.0, ingredient_id=77)]

    picks = s8_top_picks.select_top(concern, {"히알루론산"}, bsti)

    assert [(c.name_kor, src) for c, src in picks] == [("히알루론산", s8_top_picks.SOURCE_BOTH)]


def test_empty_both_axes_yields_nothing():
    assert s8_top_picks.select_top([], set(), []) == []


def test_card_carries_match_source():
    """⑩ 이 종합 카드에 출처를 실어 프론트가 근거를 구분할 수 있게 한다."""
    picks = [
        (_cand("판테놀"), s8_top_picks.SOURCE_BOTH),
        (_cand("세라마이드"), s8_top_picks.SOURCE_BSTI),
    ]

    cards = s10_response._top_ingredients(picks, UserContext(user_id="u"), {})

    assert [(c.name_kor, c.match_source) for c in cards] == [
        ("판테놀", "both"),
        ("세라마이드", "bsti"),
    ]


def test_match_source_is_opt_in():
    """출처는 ⑧을 거친 카드에만 붙는다 — 축을 모르는 경로가 임의 값을 채우면 안 된다."""
    cards = [s10_response._evidence_from_candidate(_cand("세라마이드"), UserContext(user_id="u"))]

    assert cards[0].match_source is None


def test_product_match_source_follows_the_ingredient_axis():
    """제품 출처는 담은 대표 성분에서 따온다 — 두 축이 섞이면 both."""
    from app.modules.recommendations.schemas import ProductRecommendation

    picks = [
        (_cand("판테놀"), s8_top_picks.SOURCE_CONCERN),
        (_cand("세라마이드"), s8_top_picks.SOURCE_BSTI),
    ]
    products = [
        ProductRecommendation(product_id=1, product_name="A", matched_ingredients=["판테놀"]),
        ProductRecommendation(
            product_id=2, product_name="B", matched_ingredients=["판테놀", "세라마이드"]
        ),
        ProductRecommendation(product_id=3, product_name="C", matched_ingredients=["무관성분"]),
    ]

    tagged = s10_response._with_match_source(products, picks)

    assert [p.match_source for p in tagged] == ["concern", "both", None]


def test_product_match_source_keeps_the_strongest_of_duplicate_names():
    """표시명이 같고 id 가 다른 후보가 함께 남으면 앞(강한 근거) 값을 지켜야 한다.

    ⑧ 순위가 both→concern→bsti 라, 뒤 값으로 덮으면 항상 근거를 깎는 방향이다.
    """
    from app.modules.recommendations.schemas import ProductRecommendation

    picks = [
        (_cand("히알루론산", ingredient_id=77), s8_top_picks.SOURCE_BOTH),
        (_cand("히알루론산", ingredient_id=78), s8_top_picks.SOURCE_BSTI),
    ]
    products = [
        ProductRecommendation(product_id=1, product_name="A", matched_ingredients=["히알루론산"])
    ]

    assert s10_response._with_match_source(products, picks)[0].match_source == "both"
