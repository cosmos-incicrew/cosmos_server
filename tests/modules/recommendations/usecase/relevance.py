"""추천 성분이 그 고민에 적합한가 — 두 축으로 잰다.

계약 검사(`contract.py`)는 구조만 본다. "형식은 맞는데 엉뚱한 성분"을 잡지 못한다.

정답은 만들지 않고 **이미 있는 것을 쓴다.** `rec_cases.recommended_ingredients` 는 상담
사례에서 전문가가 그 고민(`target_concern`)에 실제로 추천한 성분이다. 고민별 30종 안팎이라
좁지만, 사람이 매긴 라벨이라는 점에서 LLM 판정보다 근거가 강하다.

두 축을 함께 보는 이유는 전문가 집합이 좁기 때문이다. 거기 없다고 틀린 추천이 아니다 —
효능 사전에 그 고민 키워드가 있으면 근거는 있는 셈이다. 둘 다 아니면 그때가 의심할 지점이다.

  전문가 일치 : 그 고민의 전문가 추천 집합에 있는가 (강한 근거)
  키워드 일치 : 효능 텍스트에 그 고민의 검색 구절이 있는가 (약한 근거)
"""

from dataclasses import dataclass

from app.common.skin_concerns import CONCERN_LABEL_BY_CODE
from app.core.supabase import get_supabase, rows
from app.modules.recommendations.constants import CONCERN_SEARCH_KEYWORDS
from app.modules.recommendations.pipeline.s8_top_picks import SOURCE_BSTI
from app.modules.recommendations.util.ingredient_names import normalize_ingredient_name
from tests.modules.recommendations.usecase.runner import Case


@dataclass
class Verdict:
    """성분 한 건의 판정."""

    name: str
    concerns: list[str]
    expert: bool  # 전문가 추천 집합에 있음
    keyword: bool  # 효능 텍스트에 고민 키워드 있음

    @property
    def grounded(self) -> bool:
        return self.expert or self.keyword


async def expert_sets() -> dict[str, set[str]]:
    """고민 라벨 → 전문가가 그 고민에 추천한 성분명 집합.

    `recommended_ingredients` 에는 한글명과 INCI 영문명이 섞여 있어(고유 138개 중 55개가
    영문) 영문은 `rec_efficacy.inci` 로 한글명을 되찾는다. 안 그러면 40% 가 미매칭이 되어
    일치율이 실제보다 낮게 나온다.
    """
    client = await get_supabase()
    inci_rows = rows(
        await client.table("rec_efficacy").select("inci, name_kor").execute()
    )
    korean_by_inci = {
        str(row["inci"]): normalize_ingredient_name(str(row["name_kor"]))
        for row in inci_rows
        if row.get("inci") and row.get("name_kor")
    }

    case_rows = rows(
        await client.table("rec_cases")
        .select("target_concern, recommended_ingredients")
        .execute()
    )
    sets: dict[str, set[str]] = {}
    for row in case_rows:
        concern = str(row.get("target_concern") or "")
        if not concern:
            continue
        bucket = sets.setdefault(concern, set())
        for raw in row.get("recommended_ingredients") or []:
            name = korean_by_inci.get(str(raw)) or normalize_ingredient_name(str(raw))
            if name:
                bucket.add(name)
    return sets


def judge(case: Case, sets: dict[str, set[str]]) -> list[Verdict]:
    """고민 축 성분만 판정한다.

    BSTI 축은 고민이 아니라 타입 기준으로 뽑히므로 "그 고민에 적합한가"를 물을 대상이
    아니다. 여기 섞으면 타입 성분이 전부 불일치로 잡혀 지표가 무의미해진다.
    """
    response = case.response
    if response is None:
        return []

    labels = [CONCERN_LABEL_BY_CODE[c] for c in case.context.concerns]
    keywords = [kw for c in case.context.concerns for kw in CONCERN_SEARCH_KEYWORDS.get(c, ())]

    verdicts = []
    for card in response.top_ingredients:
        if card.match_source == SOURCE_BSTI:
            continue
        text = f"{card.efficacy or ''} {card.safety_note or ''}"
        verdicts.append(
            Verdict(
                name=card.name_kor,
                concerns=case.context.concerns,
                expert=any(card.name_kor in sets.get(label, set()) for label in labels),
                keyword=any(kw in text for kw in keywords),
            )
        )
    return verdicts


def summarize(verdicts: list[Verdict]) -> str:
    if not verdicts:
        return "판정 대상 없음"
    total = len(verdicts)
    expert = sum(1 for v in verdicts if v.expert)
    keyword = sum(1 for v in verdicts if v.keyword)
    grounded = sum(1 for v in verdicts if v.grounded)
    return (
        f"고민 축 성분 {total}건 · 전문가 일치 {expert}건({expert / total:.0%}) · "
        f"키워드 일치 {keyword}건({keyword / total:.0%}) · "
        f"근거 있음 {grounded}건({grounded / total:.0%})"
    )
