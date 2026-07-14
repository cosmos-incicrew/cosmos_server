"""피부 고민 8종 표준 코드 — 온보딩·추천·데이터 적재 공용 단일 소스.

DB(`rec_cases.target_concern`)는 AI Hub 원본 한글 라벨을 그대로 저장하고, 앱
계층은 영문 코드로 다룬다. 라벨은 AI Hub 71886 `target_concern` 필드값과 1:1이며
`rec_cases_target_concern_check` 제약과 정확히 일치해야 한다 (2026-07-13 확정).
"""

from typing import Final

# 코드 → AI Hub 원본 한글 라벨 (DB CHECK 제약과 문자열까지 일치)
CONCERN_LABEL_BY_CODE: Final[dict[str, str]] = {
    "pores": "모공",
    "brightening": "미백(색소침착/기미/칙칙함)",
    "wrinkles": "주름",
    "acne": "여드름/뾰루지",
    "redness": "붉어짐(홍조)",
    "dryness": "과각질/악건성",
    "sensitivity": "민감성(트러블/자극감)",
    "sagging": "피부처짐/탄력저하",
}

# 라벨 → 코드 (적재 시 원본 라벨을 코드로 되돌릴 때)
CONCERN_CODE_BY_LABEL: Final[dict[str, str]] = {
    label: code for code, label in CONCERN_LABEL_BY_CODE.items()
}

CONCERN_CODES: Final[tuple[str, ...]] = tuple(CONCERN_LABEL_BY_CODE)
CONCERN_LABELS: Final[tuple[str, ...]] = tuple(CONCERN_LABEL_BY_CODE.values())
