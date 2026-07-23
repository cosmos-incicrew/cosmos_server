"""⑤ 안전성 필터 — "위험하거나 주의가 필요한 성분을 거른다".

검사 7종: 사용제한(금지/한도) · 미매핑 명칭 보조 · 민감(S) 축 · 착향 알레르기 ·
성분 주의사항 · 임신·수유 금기 · 고민-성분 상충. 설계 01 §2-⑤.

사용자 조건으로 판정이 갈리는 검사가 둘이다 — 임신·수유(제외/경고 2단)와 BSTI 민감(S)
축(한도 + 자극 서술이면 제외, 04 §12). 둘 다 국내 고시에 해당 축이 없어 사용자에게
"식약처 기준"으로 표시해서는 안 된다.

**이 단계는 우아한 축소 대상이 아니다.** BSTI·화장대는 없으면 생략해도 되지만
사용제한 조회가 실패한 것을 "제한 없음"으로 읽으면 금지 성분이 무경고로 나간다.
그래서 조회 성공 여부(`RestrictionLookup.ok`)를 값으로 들고 다닌다.
"""

import logging
from typing import Any, NamedTuple

from app.core.supabase import get_supabase, rows
from app.modules.recommendations import bsti_traits
from app.modules.recommendations.constants import (
    ALLERGEN_INGREDIENTS,
    CONFLICTING_CONCERNS,
    PREGNANCY_AVOID,
    PREGNANCY_CAUTION,
)
from app.modules.recommendations.schemas import Candidate, IngredientWarning, UserContext
from app.modules.recommendations.util.ingredient_names import normalize_ingredient_name

logger = logging.getLogger(__name__)

_RESTRICTION_COLUMNS = "ingredient_id, name_kor, notice_ingr_name, regulate_type, limit_cond"
_BANNED = "금지"

# 데이터에 "없음"·"-" 같은 자리채움이 들어 있어 그대로 경고에 붙이면
# "…/ 0.01~1% / 없음" 처럼 의미 없는 문구가 사용자에게 노출된다.
_EMPTY_NOTE_VALUES = frozenset({"없음", "해당없음", "-", "n/a", "na", "없슴"})

# 경고 타입은 하나로 둔다. 제외된 성분은 응답에 아예 없으므로 프론트가 제외/경고를
# 구분할 필요가 없고, "금기"는 근거상 사실이 아니라 "주의"로 이름을 맞췄다.
_PREGNANCY_WARNING = "임신수유주의"
_AVOID_TEXT = (
    "임신·수유 중에는 사용이 권고되지 않는 성분입니다. 사용 중이시라면 전문가와 상의해 주세요."
)
_CAUTION_TEXT = (
    "화장품에 쓰이는 농도에서는 임신 중 사용이 대체로 안전하다고 보지만, "
    "제품의 함량을 확인하시고 걱정되면 전문가와 상의해 주세요."
)

# 민감(S) 축 사용자용 자극 판정 어휘 — **근거·실측은 04 §12**. 여기 요약만 둔다.
# (constants ⑤절로 옮길 대상. 지금은 다른 담당자가 constants.py 를 고치는 중이라 여기 둔다)
#
#   · 국내 규제 근거는 없다. 식약처 고시에 "민감성 피부" 축이 없어, 판정은
#     식약처 사용한도 등재(객관 사실) + 성분 사전 서술(rec_efficacy.safety_note)의
#     **조합**이다. 사용자에게 "식약처 기준"으로 표시하면 안 된다 — PREGNANCY_AVOID 와
#     같은 함정이다.
#   · 서술만으로는 못 쓴다. `자극` 단독 매칭은 2,270행 중 869행(38%)에 걸리고 그 대부분이
#     "자극이 적다"는 **안전 서술**이다. 사용한도 등재(45종)와 AND 로 묶어 26종까지 좁혔다.
#   · 원문에 한국어와 영어가 섞여 있어 한쪽만 보면 놓친다 — 레조시놀의 자극 서술은
#     영어 문장("especially in sensitive skin types")에만 있다.
_IRRITATION_TERMS = ("자극", "알레르기", "알러지", "irritat", "allerg", "sensitiz")

# 안전 서술이 하나라도 있으면 자극 판정을 물린다 — 같은 칸에 위험·안전 서술이 섞여 있을 때
# (페녹시에탄올) 남기는 쪽으로 기운다. 과차단이 민감성 사용자에게 더 비싸기 때문이다.
_REASSURANCE_TERMS = (
    "자극이 적",
    "자극은 적",
    "자극 적",
    "자극성 낮",
    "자극이 없",
    "저자극",
    "민감한 피부에도",
    "민감성 피부에도",
    "알레르기 반응이 거의 없",
    "안전하게 사용",
    "위험이 낮",
    "가능성이 낮",
    "non-irritating",
    "nonirritating",
    "non-sensitizing",
    "well tolerated",
    "well-tolerated",
    "safe for sensitive",
    "minimal risk of irritation",
    "low risk of irritation",
)


class RestrictionLookup(NamedTuple):
    """사용제한 조회 결과. `ok=False` 는 "제한 없음"이 아니라 "확인 못 함"이다."""

    by_id: dict[int, dict[str, Any]]
    by_name: dict[str, dict[str, Any]]
    ok: bool


async def apply_safety_filters(
    candidates: list[Candidate], context: UserContext
) -> list[Candidate]:
    """금지 성분은 빼고 주의가 필요한 성분에는 경고를 붙인다."""
    lookup = await fetch_restrictions(candidates)
    profile = _SafetyProfile.of(context)

    kept: list[Candidate] = []
    for candidate in candidates:
        # 상류(④)가 정규화했다고 믿지 않고 여기서 다시 키를 만든다. 안전 검사는
        # 호출 경로가 하나 늘 때마다 조용히 우회되면 안 되는 경계다.
        key = normalize_ingredient_name(candidate.name_kor)

        if _drop_for_pregnancy(candidate, key, profile):
            continue
        if _drop_for_restriction(candidate, key, lookup, profile):
            continue

        _warn_allergen(candidate, key, profile)
        _warn_notes(candidate)
        _warn_concern_conflict(candidate, context.concerns)
        kept.append(candidate)
    return kept


class _SafetyProfile(NamedTuple):
    """사용자 쪽 판정 조건을 한 번만 계산해 들고 다닌다."""

    expecting: bool
    unknown_pregnancy: bool
    # BSTI 민감(S) 축. 알레르기 경고 강조와 자극 성분 제외가 함께 읽는다.
    sensitive: bool

    @classmethod
    def of(cls, context: UserContext) -> "_SafetyProfile":
        expecting = bool(context.is_pregnant) or bool(context.is_nursing)
        # 한쪽만 수집된 상태(예: is_pregnant=False, is_nursing=None)도 unknown 이다.
        # `둘 다 None` 으로만 판정하면 그 사이 구간이 제거도 경고도 안 되는 구멍이 된다.
        unknown = not expecting and (context.is_pregnant is None or context.is_nursing is None)
        return cls(
            expecting=expecting,
            unknown_pregnancy=unknown,
            sensitive=bsti_traits.is_sensitive(context.bsti_type),
        )


def _drop_for_pregnancy(candidate: Candidate, key: str, profile: _SafetyProfile) -> bool:
    """임신·수유 관련 판정. 근거 강도에 따라 제외와 경고를 나눈다 (constants 주석 참조).

    제외 대상도 임신·수유가 **확인된** 경우에만 뺀다. 미수집(unknown) 상태에서 전
    사용자를 일괄 제거하면 핵심 성분이 막혀 과차단이 된다.
    """
    if key in PREGNANCY_AVOID:
        if profile.expecting:
            logger.info("임신·수유 중 권고되지 않는 성분으로 후보 제외: %s", key)
            return True
        if profile.unknown_pregnancy:
            candidate.warnings.append(IngredientWarning(type=_PREGNANCY_WARNING, text=_AVOID_TEXT))
    elif key in PREGNANCY_CAUTION and (profile.expecting or profile.unknown_pregnancy):
        candidate.warnings.append(IngredientWarning(type=_PREGNANCY_WARNING, text=_CAUTION_TEXT))
    return False


def _drop_for_restriction(
    candidate: Candidate, key: str, lookup: RestrictionLookup, profile: _SafetyProfile
) -> bool:
    """식약처 사용제한: 금지는 제거, 한도는 경고, 확인 불가는 그 사실을 경고로 남긴다.

    한도 성분은 민감(S) 축 사용자에게만 한 겹 더 본다 — 제품 내 실제 함량을 우리가 모르는
    상태라 "한도 안에서 쓰였을 것"이라는 가정 위에 경고만 붙이는데, 자극 서술까지 있는
    성분이면 그 가정이 틀렸을 때 손해를 보는 쪽이 민감성 피부다. 실제로 레조시놀(한국
    기타제품 0.1%·캐나다 금지)이 DSPW 사용자의 대표 추천 3개에 들어갔다 (04 §12).
    """
    restriction = None
    if candidate.ingredient_id is not None:
        restriction = lookup.by_id.get(candidate.ingredient_id)
    restriction = restriction or lookup.by_name.get(key)

    if restriction:
        if restriction.get("regulate_type") == _BANNED:
            logger.info(
                "사용제한(금지)으로 후보 제외: %s (ingredient_id=%s)", key, candidate.ingredient_id
            )
            return True
        if profile.sensitive and _states_irritation_risk(candidate.safety_note):
            logger.info("민감(S) 축 — 사용한도 + 자극 서술로 후보 제외: %s", key)
            return True
        candidate.warnings.append(
            IngredientWarning(
                type="한도",
                text=restriction.get("limit_cond") or "사용 한도가 있는 성분입니다.",
            )
        )
        return False

    if not lookup.ok or candidate.ingredient_id is None:
        # 조회 실패를 "제한 없음"으로 읽으면 금지 성분이 무경고로 나간다.
        candidate.warnings.append(
            IngredientWarning(
                type="안전성확인불가",
                text="식약처 원료 정보와 연결되지 않아 안전성을 확인하지 못했습니다.",
            )
        )
    return False


def _states_irritation_risk(note: str | None) -> bool:
    """성분 사전의 주의 서술이 자극·알레르기 **위험**을 말하는가.

    영어 원문이 섞여 있어 소문자로 눕혀 두 언어를 한 번에 본다.
    """
    if not note:
        return False
    text = note.lower()
    if any(term in text for term in _REASSURANCE_TERMS):
        return False
    return any(term in text for term in _IRRITATION_TERMS)


def _warn_allergen(candidate: Candidate, key: str, profile: _SafetyProfile) -> None:
    if key not in ALLERGEN_INGREDIENTS:
        return
    text = "착향제 알레르기 유발성분입니다."
    if profile.sensitive:
        text += " 민감성 피부는 사용 전 첩포 검사를 권장합니다."
    candidate.warnings.append(IngredientWarning(type="알레르기유발", text=text))


def _warn_notes(candidate: Candidate) -> None:
    """주의사항·권장농도·배합규제를 한 줄로 합친다.

    각각 경고로 붙이면 한 성분에 "주의사항"이 3줄 쌓여 화면이 흐려진다.
    """
    notes = [
        note
        for note in (
            candidate.safety_note,
            candidate.recommended_concentration,
            candidate.regulation_note,
        )
        if note is not None and _is_meaningful_note(note)
    ]
    if notes:
        candidate.warnings.append(IngredientWarning(type="주의사항", text=" / ".join(notes)))


def _warn_concern_conflict(candidate: Candidate, concerns: list[str]) -> None:
    """권장 피부타입이 사용자의 동반 고민과 상충하면 경고한다."""
    skin_types = candidate.recommended_skin_types or ""
    for skin_type, conflicting in CONFLICTING_CONCERNS.items():
        if skin_type in skin_types and any(c in concerns for c in conflicting):
            text = f"{skin_type} 피부에 권장되는 성분이라 다른 고민에는 자극이 될 수 있습니다."
            candidate.warnings.append(IngredientWarning(type="고민상충", text=text))
            return


def _is_meaningful_note(note: str | None) -> bool:
    if note is None:
        return False
    stripped = note.strip()
    return bool(stripped) and stripped.lower() not in _EMPTY_NOTE_VALUES


def _worst(restriction_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """한 성분의 여러 규제 행 중 가장 강한 것을 고른다.

    `restrictions.ingredient_id` 에는 UNIQUE 가 없고 한 성분이 여러 조항을 갖는 것이
    정상이다. dict 로 덮어쓰면 마지막 행만 남아 `금지`가 `한도`로 뒤집힐 수 있다.
    """
    for row in restriction_rows:
        if row.get("regulate_type") == _BANNED:
            return row
    return restriction_rows[0]


async def fetch_restrictions(candidates: list[Candidate]) -> RestrictionLookup:
    """사용제한 정보를 id·명칭 두 갈래로 읽는다 (미매핑 후보의 보조 검사용)."""
    ids = [c.ingredient_id for c in candidates if c.ingredient_id is not None]
    names = [normalize_ingredient_name(c.name_kor) for c in candidates]
    grouped_by_id: dict[int, list[dict[str, Any]]] = {}
    grouped_by_name: dict[str, list[dict[str, Any]]] = {}
    try:
        client = await get_supabase()
        if ids:
            for row in rows(
                await client.table("restrictions")
                .select(_RESTRICTION_COLUMNS)
                .in_("ingredient_id", ids)
                .execute()
            ):
                if row.get("ingredient_id"):
                    grouped_by_id.setdefault(int(row["ingredient_id"]), []).append(row)
        if names:
            # 고시 원문 명칭(notice_ingr_name)만 등재된 행이 있어 name_kor 만 보면
            # 미매핑 후보의 보조 검사에 구멍이 생긴다 (01 §2-⑤).
            #
            # 두 컬럼을 or_ 한 줄로 묶으면 성분명을 필터 문자열에 직접 조립해야 한다.
            # `,` 나 `"` 가 든 이름 하나로 필터가 깨지면 조회가 **조용히 0건**이 되고,
            # lookup.ok 는 True 라서 금지 성분이 무경고로 통과한다. 왕복 1회를 더 쓰더라도
            # in_() 두 번으로 나눠 문자열 조립 자체를 없앤다.
            for column in ("name_kor", "notice_ingr_name"):
                for row in rows(
                    await client.table("restrictions")
                    .select(_RESTRICTION_COLUMNS)
                    .in_(column, names)
                    .execute()
                ):
                    key = row.get(column)
                    if key:
                        # 조회 키와 저장 키를 같은 정규화로 맞춘다. 한쪽만 정규화하면
                        # DB 값에 괄호·개행이 붙은 순간 안전 검사가 빗나간다 (names.py).
                        grouped_by_name.setdefault(normalize_ingredient_name(str(key)), []).append(
                            row
                        )
    except Exception:
        logger.error("사용제한 조회 실패 — 전 후보에 안전성확인불가 경고 부착", exc_info=True)
        return RestrictionLookup({}, {}, ok=False)

    return RestrictionLookup(
        {iid: _worst(group) for iid, group in grouped_by_id.items()},
        {name: _worst(group) for name, group in grouped_by_name.items()},
        ok=True,
    )
