"""② 질의 구성 — "무엇을 검색할지 검색어를 만든다".

고민별 검색 질의를 템플릿으로 만든다. 나이·성별·BSTI 축 서술을 조합하고 없는
요소는 생략한다. 예: "30대 여성 지성·민감 경향 피부의 모공 관리에 도움되는 성분".
설계 01 §2-②.
"""

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.modules.recommendations import bsti_traits
from app.modules.recommendations.schemas import UserContext

_AGE_BUCKET = 10  # "32세" → "30대"

_GENDER_LABELS = {"female": "여성", "male": "남성"}


def build_queries(context: UserContext) -> list[tuple[str, str]]:
    """(고민 코드, 검색 질의) 목록을 만든다. 고민 하나당 하나."""
    prefix = " ".join(_describe_person(context))
    # prefix 가 비면 "의 모공 관리에…" 처럼 조사가 앞에 남는다.
    lead = f"{prefix}의 " if prefix else ""
    return [
        (code, f"{lead}{CONCERN_LABEL_BY_CODE[code]} 관리에 도움되는 성분")
        for code in context.concerns
    ]


def _describe_person(context: UserContext) -> list[str]:
    """검색어 앞에 붙일 사람 묘사. 값이 없는 요소는 넣지 않는다."""
    parts: list[str] = []
    if context.age:
        parts.append(f"{context.age // _AGE_BUCKET * _AGE_BUCKET}대")
    gender = _GENDER_LABELS.get(context.gender or "")
    if gender:
        parts.append(gender)
    skin = bsti_traits.describe(context.bsti_type)
    if skin:
        parts.append(skin)
    return parts
