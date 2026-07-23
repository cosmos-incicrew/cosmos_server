"""효능·주의 표시 텍스트 정제 — ⑩ 응답 조립이 쓰는 단일 출처.

`names.normalize_ingredient_name` 이 성분명에 대해 그렇듯, 서술형 텍스트도 정제 규칙을
한 곳에 모은다. 여러 조립 지점에 흩뿌리면 한쪽만 고쳐져 같은 원본이 화면마다 다르게
나간다.

`rec_efficacy.efficacy`·`safety_note` 원본에는 두 가지가 섞여 있다 (실측 2,270행):
개행으로 이어붙인 여러 조각(그대로 내보내면 한 줄 카드가 두 줄로 깨진다)과, 한국어
설명 뒤에 통째로 붙은 영어 원문(English-only 문장 431개). 이 서비스는 한국어 전용이라
영어 원문은 사용자에게 의미가 없다.

영어 조각을 **지우는 것은 최후 수단**이다. 영어에만 있는 정보가 통째로 사라지기
때문이다 — 레조시놀 `safety_note` 의 "especially in sensitive skin types"(민감성 피부
경고)가 그렇게 잘려 나가, 정작 민감성 BSTI 사용자에게 그 경고가 닿지 않았다. 그래서
⑥이 조각 단위로 번역하고 ⑩이 제자리에 끼워 넣는다(`apply_translations`). 삭제는 번역이
없거나 믿을 수 없을 때의 안전망으로만 남는다(`clean_display_text`).
"""

import re

_HANGUL = re.compile(r"[가-힣]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_WHITESPACE = re.compile(r"\s+")

# 문장 경계 = 종결부호 뒤 공백, 개행, 또는 종결부호에 곧바로 붙은 대문자.
# 마지막 갈래가 필요한 이유는 원본에 공백 없이 이어붙은 영어가 있기 때문이다
# (`…있을 수 있습니다.Safe for sensitive skin…`). 공백만 경계로 보면 이 조각이
# 한국어와 한 덩어리가 되어 영어 판정을 통째로 빠져나간다.
# 숫자 뒤 대문자는 경계가 아니라 `0.1~1.0%` 같은 표기는 그대로 남는다.
# 한글 바로 뒤에 붙은 대문자도 경계다 — `기능성화장품 미백 고시원료Niacinamide is well
# tolerated…` 처럼 구분자 없이 이어붙인 행이 있고, 경계로 안 보면 영어가 한국어와 한
# 덩어리가 되어 판정을 빠져나간다. `비타민C` 는 잘리지만 한 글자라 영어 판정 대상이
# 아니라서 그대로 남는다.
_SENTENCE_BREAK = re.compile(
    r"(?<=[.!?])\s+|\n+|(?<=[.!?])(?=[A-Z])|(?<=[가-힣])(?=[A-Z])"
)


def is_english_only(text: str | None) -> bool:
    """영어 전용 판정 — 한글이 하나도 없고 영단어가 있는 텍스트(또는 문장 조각).

    한글이 한 글자라도 있으면 False 다. 오탐이 나면 한국어 정보가 사라지므로 판정을
    보수적으로 잡았다: 성분명 병기(`락토바이오닉애씨드(Lactobionic Acid)은 …`),
    영문 약어 혼용(`patch test 가 권장됩니다`), 단위·농도 표기가 섞인 한국어 문장이
    전부 이 조건에서 살아남는다.

    영단어를 2자 이상으로 요구하는 이유는 한글도 영문도 없는 조각(`0.1~1.0%`)을
    지우지 않기 위해서다. 실데이터 7,110조각에 이 규칙을 돌리면 431개가 제거되는데,
    "영단어 3개 이상"으로 더 조여도 결과가 같아 더 조일 이유가 없었다.

    공개 함수인 이유는 판정 주체가 둘이기 때문이다 — ⑥이 "번역시킬 조각"을 고르고
    ⑩이 "번역본으로 갈아끼울 조각"을 고른다. 두 판정이 갈리면 번역을 요청해 놓고 안
    쓰거나, 요청하지 않은 자리를 LLM 출력으로 덮는다.
    """
    if not text:
        return False
    return not _HANGUL.search(text) and bool(_LATIN_WORD.search(text))


def _split(text: str) -> list[str]:
    return [s for s in (s.strip() for s in _SENTENCE_BREAK.split(text)) if s]


def english_segments(text: str | None) -> list[str]:
    """번역이 필요한 영어 조각을 원문 순서대로 돌려준다 (⑥ 프롬프트용).

    ⑩의 `apply_translations` 와 같은 분할·판정을 쓴다. 개수가 곧 계약이라(요청한 수 =
    돌려받아야 할 수) 분할이 갈리면 번역이 통째로 폐기된다.
    """
    if not text:
        return []
    return [s for s in _split(text) if is_english_only(s)]


def apply_translations(text: str | None, translations: list[str]) -> str | None:
    """영어 조각만 번역본으로 제자리 치환한다. 한국어 조각은 손대지 않는다.

    개수가 안 맞거나 번역본이 비었거나 여전히 영어면 **전량 폐기**하고
    `clean_display_text` 로 떨어진다 — 자리가 밀린 채 끼워 넣으면 다른 문장 자리에 엉뚱한
    번역이 들어가고, 그게 `safety_note` 면 안전 정보가 뒤바뀐다.
    """
    if text is None:
        return None
    segments = _split(text)
    targets = [i for i, s in enumerate(segments) if is_english_only(s)]
    if len(targets) != len(translations) or any(
        not t.strip() or is_english_only(t) for t in translations
    ):
        return clean_display_text(text)
    for index, translated in zip(targets, translations, strict=True):
        segments[index] = translated.strip()
    return _WHITESPACE.sub(" ", " ".join(segments)) or None


def clean_display_text(text: str | None) -> str | None:
    """개행을 없애고 영어 전용 문장을 걷어낸 한 줄 표시 텍스트를 만든다 (번역 실패 시 안전망).

    전부 영어면 **빈 값**이다. 예전엔 "빈 칸이 영어보다 나쁘다"고 보고 원문을 남겼는데,
    실측에서 그 경로가 실제로 열렸다 — 병풀추출물(BSTI 16타입 중 8개에 등장)의 주의가
    통째로 영어라 ⑥ 번역이 조각 수를 못 맞추자 영어가 그대로 사용자에게 나갔다.
    한국어 전용 서비스에서 읽을 수 없는 문장은 정보가 아니다.
    """
    if text is None:
        return None
    kept = [s for s in _split(text) if not is_english_only(s)]
    return _WHITESPACE.sub(" ", " ".join(kept)) or None
