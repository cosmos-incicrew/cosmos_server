"""BSTI 타입 코드 → 피부 특성 해석.

`OSPW` 같은 4글자 코드를 "지성·민감·색소침착 경향·주름 경향 피부"로 푼다. 검색 질의
구성(②)과 민감성 판정(⑤)이 쓴다.

16타입 dict 가 아니라 **축별 특성 서술 8개 엔트리**로 두고 코드를 분해해 조합한다
(박금별 "BSTI 근거 및 성분정보" 페이지의 축 정의) — 16개를 나열하면 축 하나가 바뀔 때
16곳을 고쳐야 한다. 타입별 권장·기피 성분 목록은 여기 두지 않는다. 박금별의 BSTI
테이블을 단일 소스로 소비한다 (01 §2-①).
"""

from typing import Final

BSTI_TYPE_CODE_LENGTH: Final = 4

# 자리 순서: 유·수분 → 민감도 → 색소 → 노화
_AXIS_TRAITS: Final[tuple[dict[str, str], ...]] = (
    {"O": "지성", "D": "건성"},
    {"S": "민감", "R": "저항"},
    {"P": "색소침착 경향", "N": "색소 안정"},
    {"W": "주름 경향", "T": "탱탱함 유지"},
)

_SENSITIVE_POLE: Final = "S"
_SENSITIVE_POSITION: Final = 1


def decode_traits(type_code: str | None) -> list[str]:
    """타입 코드 4글자를 축별 특성 서술 목록으로 분해한다. 모르는 글자는 건너뛴다."""
    if not type_code or len(type_code) != BSTI_TYPE_CODE_LENGTH:
        return []
    code = type_code.upper()
    return [
        traits[letter]
        for letter, traits in zip(code, _AXIS_TRAITS, strict=True)
        if letter in traits
    ]


def describe(type_code: str | None) -> str:
    """검색 질의에 넣을 한 문장. 예: OSPW → "지성·민감·색소침착 경향·주름 경향 피부"."""
    traits = decode_traits(type_code)
    return f"{'·'.join(traits)} 피부" if traits else ""


def is_sensitive(type_code: str | None) -> bool:
    """민감(S) 축 여부 — 알레르기 경고를 강조할지 판단한다 (01 §2-⑤)."""
    if not type_code or len(type_code) != BSTI_TYPE_CODE_LENGTH:
        return False
    return type_code.upper()[_SENSITIVE_POSITION] == _SENSITIVE_POLE
