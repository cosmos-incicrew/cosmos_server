"""응답 계약 검사 — 위반을 문자열 목록으로 돌려준다 (빈 목록 = 통과).

예외가 아니라 목록인 이유는 한 응답에서 위반이 여럿 나오기 때문이다. 첫 건에서 멈추면
16타입을 한 번에 훑는 의미가 없다.

검사 항목은 전부 **실제로 새어 나간 적이 있는 것**이다. 가설로 늘리지 않는다 — 늘리면
통과 여부가 아니라 검사기 자체를 디버깅하게 된다.
"""

import re
from typing import Any

from app.modules.recommendations.constants import (
    DISCLAIMER,
    MAX_RECOMMENDED_PRODUCTS,
    MAX_TOP_INGREDIENTS,
)
from app.modules.recommendations.pipeline.s8_top_picks import (
    SOURCE_BOTH,
    SOURCE_BSTI,
    SOURCE_CONCERN,
)
from app.modules.recommendations.schemas import RecommendationResponse, UserContext
from app.modules.recommendations.util.display_text import is_english_only

# 노션 응답 계약(2026-07-23)의 최상위 키. 순서까지 계약이다 — 프론트가 문서와 눈으로
# 대조하고, pydantic 은 선언 순서로 직렬화한다.
EXPECTED_KEYS: tuple[str, ...] = (
    "status",
    "answer",
    "cases",
    "top_ingredients",
    "top_products",
    "advisory",
    "user_profile",
    "disclaimer",
)

_SOURCES = {SOURCE_BOTH, SOURCE_CONCERN, SOURCE_BSTI}
_WARNING_TYPES = {
    "한도",
    "알레르기유발",
    "주의사항",
    "안전성확인불가",
    "임신수유주의",
    "고민상충",
}
_ADVISORY_CODES = {"weak_evidence", "no_evidence", "no_candidates", "partial_evidence"}
_ADVISORY_ACTIONS = {"take_bsti", "retry_with_other_concerns", "retry_later"}

# 표시 텍스트에 남으면 안 되는 것 — 개행은 카드를 두 줄로 깨고, 앞뒤 공백은 JSON 에 그대로 실린다.
_NEWLINE = re.compile(r"[\r\n]")


def check(response: RecommendationResponse, context: UserContext) -> list[str]:
    """응답 하나를 검사한다. 반환값이 비어 있으면 계약을 지킨 것이다."""
    problems: list[str] = []
    payload = response.model_dump()

    if tuple(payload) != EXPECTED_KEYS:
        problems.append(f"최상위 키가 계약과 다르다: {tuple(payload)}")
    if response.disclaimer != DISCLAIMER:
        problems.append("disclaimer 가 고정 문구와 다르다")

    problems += _check_status(response)
    problems += _check_profile(response, context)
    problems += _check_ingredients(response)
    problems += _check_products(response)
    problems += _check_cases(response)
    problems += _check_advisory(response)
    return problems


def _check_status(response: RecommendationResponse) -> list[str]:
    if response.status == "insufficient_evidence":
        # 근거 부족은 에러가 아니라 정형 응답이다 — 서사·목록이 비고 사유가 반드시 붙는다.
        problems = []
        if response.answer is not None:
            problems.append("insufficient_evidence 인데 answer 가 있다")
        if response.top_ingredients or response.top_products or response.cases:
            problems.append("insufficient_evidence 인데 목록이 비어 있지 않다")
        if response.advisory is None:
            problems.append("insufficient_evidence 인데 advisory 가 없다 (사유 미전달)")
        return problems

    if response.status != "ok":
        return [f"status 값이 계약 밖이다: {response.status}"]

    answer = response.answer
    if answer is None:
        return ["status=ok 인데 answer 가 없다"]
    problems = [
        f"answer.{field} 가 비어 있다"
        for field, text in (
            ("cause_analysis", answer.cause_analysis),
            ("recommendation", answer.recommendation),
            ("usage_guide", answer.usage_guide),
        )
        if not text.strip()
    ]
    if not response.top_ingredients:
        # 서사만 나가고 메인 카드가 비면 프론트가 그릴 게 없다 (실제로 났던 상태).
        problems.append("status=ok 인데 top_ingredients 가 비었다")
    return problems


def _check_profile(response: RecommendationResponse, context: UserContext) -> list[str]:
    profile = response.user_profile
    mismatches = [
        f"user_profile.{field} 가 입력과 다르다 ({got!r} != {want!r})"
        for field, got, want in (
            ("age", profile.age, context.age),
            ("gender", profile.gender, context.gender),
            ("bsti_type", profile.bsti_type, context.bsti_type),
            ("concerns", profile.concerns, context.concerns),
        )
        if got != want
    ]
    return mismatches


def _check_text(label: str, text: str | None, *, korean_only: bool = True) -> list[str]:
    """표시 텍스트 공통 검사.

    `korean_only=False` 는 INCI 전용이다 — INCI 는 국제 표준 영문 성분명이라 영어인 것이
    정상이고(`Hexapeptide-2`), 응답 계약도 "영문명(INCI)"으로 정의한다. 나머지 서술형
    텍스트는 한국어 전용 서비스라 영어 전용 문장이 나가면 안 된다.
    """
    if not text:
        return []
    problems = []
    if _NEWLINE.search(text):
        problems.append(f"{label} 에 개행이 남아 있다")
    if text != text.strip():
        problems.append(f"{label} 앞뒤에 공백이 있다")
    if korean_only and is_english_only(text):
        problems.append(f"{label} 가 영어 전용이다: {text[:60]!r}")
    return problems


def _check_ingredients(response: RecommendationResponse) -> list[str]:
    problems: list[str] = []
    cards = response.top_ingredients
    if len(cards) > MAX_TOP_INGREDIENTS:
        problems.append(f"top_ingredients 가 상한을 넘었다: {len(cards)}")

    seen: set[str] = set()
    for card in cards:
        label = f"top_ingredients[{card.name_kor}]"
        if card.name_kor in seen:
            problems.append(f"{label} 성분이 중복됐다")
        seen.add(card.name_kor)

        if card.match_source not in _SOURCES:
            problems.append(f"{label}.match_source 가 계약 밖이다: {card.match_source!r}")
        # BSTI 축은 표 확정 매칭이라 검색 유사도가 없다. 상수 1.0 을 실으면 프론트가
        # 신뢰도로 표시할 때 고민 축(0.7~0.85)보다 정확해 보인다.
        if card.match_source == SOURCE_BSTI and card.similarity is not None:
            problems.append(f"{label}.similarity 는 BSTI 축이라 null 이어야 한다")
        if card.match_source != SOURCE_BSTI and not (
            card.similarity is not None and 0.0 <= card.similarity <= 1.0
        ):
            problems.append(f"{label}.similarity 가 0~1 범위 밖이다: {card.similarity!r}")

        problems += _check_text(f"{label}.name_kor", card.name_kor)
        problems += _check_text(f"{label}.inci", card.inci, korean_only=False)
        problems += _check_text(f"{label}.efficacy", card.efficacy)
        problems += _check_text(f"{label}.safety_note", card.safety_note)
        problems += [
            f"{label}.warnings 에 계약 밖 타입이 있다: {w.type!r}"
            for w in card.warnings
            if w.type not in _WARNING_TYPES
        ]
    return problems


def _check_products(response: RecommendationResponse) -> list[str]:
    problems: list[str] = []
    products = response.top_products
    if len(products) > MAX_RECOMMENDED_PRODUCTS:
        problems.append(f"top_products 가 상한을 넘었다: {len(products)}")

    names = {card.name_kor for card in response.top_ingredients}
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for product in products:
        label = f"top_products[{product.product_name}]"
        if product.product_id in seen_ids:
            problems.append(f"{label} product_id 가 중복됐다")
        seen_ids.add(product.product_id)
        # 올리브영은 용량·기획 구성만 다른 행을 따로 둔다 — 사용자에겐 같은 제품이다.
        key = "".join(product.product_name.split())
        if key in seen_names:
            problems.append(f"{label} 표시명이 중복됐다")
        seen_names.add(key)

        if product.match_source is not None and product.match_source not in _SOURCES:
            problems.append(f"{label}.match_source 가 계약 밖이다: {product.match_source!r}")
        if not product.product_name.strip():
            problems.append(f"{label} 표시명이 비었다")
        # 제품은 대표 성분으로만 조회하므로 그 밖의 이름이 붙을 수 없다.
        stray = [n for n in product.matched_ingredients if n not in names]
        if stray:
            problems.append(f"{label}.matched_ingredients 에 대표 성분 밖 이름: {stray}")
        if not product.matched_ingredients:
            problems.append(f"{label} 이 담은 추천 성분이 없다")
    return problems


def _check_cases(response: RecommendationResponse) -> list[str]:
    """같은 정보를 주는 사례가 두 번 실리면 사용자에겐 중복 카드다."""
    problems: list[str] = []
    seen: set[Any] = set()
    for case in response.cases:
        key = (
            case.target_concern,
            case.gender,
            case.age,
            case.skin_type,
            tuple(sorted(case.recommended_ingredients)),
        )
        if case.id in seen or key in seen:
            problems.append(f"cases[{case.id}] 가 앞선 사례와 같은 정보다")
        seen.add(case.id)
        seen.add(key)
        if not (0.0 <= case.similarity <= 1.0):
            problems.append(f"cases[{case.id}].similarity 가 0~1 범위 밖이다")
    return problems


def _check_advisory(response: RecommendationResponse) -> list[str]:
    advisory = response.advisory
    if advisory is None:
        return []
    problems = []
    if advisory.code not in _ADVISORY_CODES:
        problems.append(f"advisory.code 가 계약 밖이다: {advisory.code!r}")
    if advisory.action is not None and advisory.action not in _ADVISORY_ACTIONS:
        problems.append(f"advisory.action 이 계약 밖이다: {advisory.action!r}")
    if not advisory.message.strip():
        problems.append("advisory.message 가 비었다")
    return problems
