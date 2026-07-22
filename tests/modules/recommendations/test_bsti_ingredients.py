"""BSTI 권장 성분 표의 무결성.

앱의 `kBstiSkinTypes` 를 옮긴 복제본이라, 손으로 고치다 한 타입이 비거나 코드가
틀리면 그 타입 사용자만 조용히 가점을 못 받는다. 화면에는 아무 표시도 안 나므로
테스트가 아니면 발견할 방법이 없다.
"""

import re

from app.modules.recommendations.bsti_ingredients import (
    _DB_ALIASES,
    BSTI_RECOMMENDED,
    recommended_for,
)
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.users.schemas import BSTI_TYPE_PATTERN


def test_covers_all_sixteen_types():
    assert len(BSTI_RECOMMENDED) == 16


def test_codes_match_db_constraint():
    """DB 제약을 통과하는 코드만 키로 둔다 — 아니면 영영 조회되지 않는다."""
    for code in BSTI_RECOMMENDED:
        assert re.fullmatch(BSTI_TYPE_PATTERN, code), code


def test_no_empty_list():
    for code, ingredients in BSTI_RECOMMENDED.items():
        assert ingredients, code


def test_names_are_prenormalized():
    """④ 가점은 정규화된 name_kor 과 정확 일치로 건다.

    표에 `살리실산 (BHA)` 처럼 괄호가 남아 있으면 그 항목만 영영 안 걸린다.
    """
    for code in BSTI_RECOMMENDED:
        for name in recommended_for(code):
            assert normalize_ingredient_name(name) == name, f"{code}: {name}"


def test_lookup_translates_to_db_spelling():
    """앱 표시명이 아니라 rec_efficacy 표기로 나와야 가점이 실제로 걸린다.

    2026-07-22 DB 실측: `살리실산`·`히알루론산`·`레티날` 같은 표시명은
    rec_efficacy 에 한 건도 없다. 번역을 빼면 19개 중 8개가 조용히 죽는다.
    """
    ospw = recommended_for("OSPW")

    assert "살리실릭애씨드" in ospw
    assert "살리실산" not in ospw
    assert "레틴알" in ospw and "바쿠치올" in ospw

    drnt = recommended_for("DRNT")
    assert "하이알루로닉애씨드" in drnt
    assert "히알루론산" not in drnt


def test_aliases_only_cover_table_entries():
    """표에 없는 이름을 번역해 두면 영영 안 쓰이는 죽은 항목이다."""
    in_table = {n for names in BSTI_RECOMMENDED.values() for n in names}

    assert set(_DB_ALIASES) <= in_table


def test_lookup_degrades_on_unknown():
    assert recommended_for(None) == []
    assert recommended_for("") == []
    assert recommended_for("OSPX") == []


def test_lookup_is_case_insensitive():
    assert recommended_for("ospw") == recommended_for("OSPW")


def test_lookup_returns_a_copy():
    """호출자가 받은 목록을 건드려도 표가 오염되면 안 된다."""
    got = recommended_for("OSPW")
    got.append("오염")

    assert "오염" not in recommended_for("OSPW")
