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

_MOISTURE_POSITION: Final = 0


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


def case_skin_type(type_code: str | None) -> str | None:
    """유·수분 축을 AI Hub 상담 사례(`rec_cases.skin_type`)의 표기로 옮긴다.

    두 데이터의 피부타입 어휘가 달라 대조하려면 매핑이 먼저 필요하다. 축 라벨(`지성`·
    `건성`)이 그 표기와 그대로 겹쳐 여기 표를 재사용한다 — 따로 두면 한쪽만 고쳐도 티가
    안 난다.

    BSTI 1축은 O/D 2극뿐이라 사례의 `복합성`·`중성`(실 데이터 8,000건 중 69%)에는 대응하는
    극이 없다. 그건 매핑 실패가 아니라 정상이므로 호출부는 None 을 "가점 없음"으로 다뤄야
    하고, 하드필터로 쓰면 사례 3분의 2가 통째로 탈락한다.
    """
    if not type_code or len(type_code) != BSTI_TYPE_CODE_LENGTH:
        return None
    return _AXIS_TRAITS[_MOISTURE_POSITION].get(type_code.upper()[_MOISTURE_POSITION])


def is_sensitive(type_code: str | None) -> bool:
    """민감(S) 축 여부 — ⑤가 알레르기 경고 강조와 자극 성분 제외에 쓴다 (04 §12)."""
    if not type_code or len(type_code) != BSTI_TYPE_CODE_LENGTH:
        return False
    return type_code.upper()[_SENSITIVE_POSITION] == _SENSITIVE_POLE
