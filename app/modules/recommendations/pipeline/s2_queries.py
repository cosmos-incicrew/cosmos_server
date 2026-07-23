"""② 질의 구성 — "무엇을 검색할지 검색어를 만든다".

고민별 검색 질의를 템플릿으로 만든다. 고민을 문두에 두고 나이·성별·BSTI 축 서술은
괄호로 뒤에 붙이며 없는 요소는 생략한다. 예: "모공에 도움되는 성분 (30대 여성 지성·민감
경향 피부)". 설계 01 §2-② · 04 §2-1.
"""

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.modules.recommendations import bsti_traits
from app.modules.recommendations.constants import GENDER_LABELS
from app.modules.recommendations.schemas import UserContext

_AGE_BUCKET = 10  # "32세" → "30대"


def build_queries(context: UserContext) -> list[tuple[str, str]]:
    """(고민 코드, 검색 질의) 목록. 고민을 문두에 두고 사람묘사는 보조로 뒤에 둔다.

    cases leg 는 이 질의를 그대로 임베딩한다. 사람묘사(BSTI 4축)를 앞세우면 색소·주름
    같은 축이 질의를 지배해 정작 입력한 고민의 사례를 밀어낸다(설계 04 §2-1). 고민을
    앞에 두어 검색을 고민 중심으로 고정한다.
    """
    descriptor = " ".join(_describe_person(context))
    tail = f" ({descriptor})" if descriptor else ""
    return [
        (code, f"{CONCERN_LABEL_BY_CODE[code]}에 도움되는 성분{tail}")
        for code in context.concerns
    ]


def _describe_person(context: UserContext) -> list[str]:
    """검색어 뒤 괄호에 붙일 사람 묘사. 값이 없는 요소는 넣지 않는다."""
    parts: list[str] = []
    if context.age:
        parts.append(f"{context.age // _AGE_BUCKET * _AGE_BUCKET}대")
    gender = GENDER_LABELS.get(context.gender or "")
    if gender:
        parts.append(gender)
    skin = bsti_traits.describe(context.bsti_type)
    if skin:
        parts.append(skin)
    return parts
