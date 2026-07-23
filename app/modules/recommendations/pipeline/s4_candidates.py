"""④ 후보 성분 집계 — "찾아온 자료에서 성분 목록을 뽑는다".

검색 결과를 성분 단위로 병합·중복 제거하고, BSTI 가점과 보유 성분 하향을 적용한 뒤
식약처 `ingredients` 와 잇는다. 설계 01 §2-④.
"""

import logging
from typing import Any

from app.core.supabase import get_supabase, rows
from app.modules.recommendations.constants import BSTI_BOOST, MAX_CANDIDATES, OWNED_PENALTY
from app.modules.recommendations.names import normalize_ingredient_name
from app.modules.recommendations.schemas import Candidate, RetrievedChunk, UserContext

logger = logging.getLogger(__name__)

# ID 매핑에 넘길 후보 수. 중복 제거로 줄어들 몫을 감안한 여유분이며, 여기서 자르는
# 이유는 조회 비용을 묶기 위해서다. 최종 상한은 MAX_CANDIDATES 로 따로 건다.
_RESOLVE_POOL = MAX_CANDIDATES * 2


async def aggregate_candidates(
    case_chunks: list[RetrievedChunk],
    efficacy_chunks: list[RetrievedChunk],
    context: UserContext,
) -> list[Candidate]:
    """성분명 기준으로 병합·중복 제거한 뒤 BSTI 가점·보유 성분 하향을 1회씩 적용한다."""
    merged: dict[str, Candidate] = {}

    for chunk in efficacy_chunks:
        name = chunk.metadata.get("name_kor") or chunk.metadata.get("inci")
        if name:
            _merge(merged, name, chunk, from_efficacy=True)

    for chunk in case_chunks:
        for name in chunk.metadata.get("recommended_ingredients") or []:
            if name:
                _merge(merged, name, chunk, from_efficacy=False)

    # 상한은 중복 제거 **뒤에** 건다. 먼저 자르면 상위권에 든 한글/INCI 중복 쌍이
    # 합쳐지면서 후보 수가 상한 아래로 떨어지고, 밀려난 후보는 복구되지 않는다.
    # 다만 ID 조회 비용을 묶으려고 넉넉한 여유분만 남기고 자른 뒤 매핑한다.
    ranked = sorted(merged.values(), key=lambda c: c.score, reverse=True)[:_RESOLVE_POOL]
    await resolve_ingredient_ids(ranked)
    resolved = _dedupe_resolved(ranked)

    # efficacy 근거 필수 (설계 04 §3-1) — 케이스에만 등장하고 효능 사전 근거가 없는
    # 성분은 추천에서 뺀다. groundedness 를 지키고, ⑩ 근거 패널(efficacy 기반)과
    # 추천 성분의 불일치(데모의 알로에신·댕댕이나무열매즙 누락)를 없앤다.
    grounded = [c for c in resolved if c.efficacy]

    # 가중치는 ID 매핑 **뒤에** 적용한다. `resolve_ingredient_ids` 가 INCI 후보의
    # 표시명을 한글로 바꾸므로, 앞에서 적용하면 영문명으로 들어온 후보(상담 사례
    # 성분의 40%)가 BSTI 가점·보유 하향을 통째로 못 받는다. ⑩의 보유 배지는 개명
    # 후 이름으로 판정하므로 "보유 표시는 되는데 하향은 안 된" 후보가 생긴다.
    _apply_personal_weights(grounded, context)
    return sorted(grounded, key=lambda c: c.score, reverse=True)[:MAX_CANDIDATES]


def _apply_personal_weights(candidates: list[Candidate], context: UserContext) -> None:
    """BSTI 권장은 올리고 이미 쓰는 성분은 내린다. dedupe 이후 각 1회만 적용된다."""
    bsti_recommended = set(context.bsti_recommended)
    owned = set(context.owned_ingredients)
    for candidate in candidates:
        if candidate.name_kor in bsti_recommended:
            candidate.score += BSTI_BOOST
        if candidate.name_kor in owned:
            candidate.score -= OWNED_PENALTY  # 제외가 아니라 하향 (긍정 피드백 보존)


def _merge(
    merged: dict[str, Candidate], name: str, chunk: RetrievedChunk, from_efficacy: bool
) -> None:
    """같은 성분이면 최고 score 를 유지하고 대응 고민·근거를 합집합으로 모은다.

    이름은 여기서 딱 한 번 정규화한다 — 이후 안전성 상수 조회·프롬프트·환각 검사·
    사용자 표시가 모두 이 키를 쓴다.
    """
    key = normalize_ingredient_name(name)
    if not key:
        return
    candidate = merged.get(key)
    if candidate is None:
        candidate = Candidate(name_kor=key, score=chunk.score)
        merged[key] = candidate
    else:
        candidate.score = max(candidate.score, chunk.score)

    concern = chunk.metadata.get("concern")
    if concern and concern not in candidate.concerns:
        candidate.concerns.append(concern)
    if chunk.source.doc_id not in candidate.source_doc_ids:
        candidate.source_doc_ids.append(chunk.source.doc_id)

    if from_efficacy:
        _fill_efficacy_fields(candidate, chunk.metadata)


def _fill_efficacy_fields(candidate: Candidate, meta: dict[str, Any]) -> None:
    """효능 인덱스에서만 오는 값들. 먼저 채워진 값을 덮어쓰지 않는다."""
    candidate.inci = candidate.inci or meta.get("inci")
    candidate.efficacy = candidate.efficacy or meta.get("efficacy")
    candidate.safety_note = candidate.safety_note or meta.get("safety_note")
    candidate.recommended_concentration = candidate.recommended_concentration or meta.get(
        "recommended_concentration"
    )
    candidate.recommended_skin_types = candidate.recommended_skin_types or meta.get(
        "recommended_skin_types"
    )
    candidate.regulation_note = candidate.regulation_note or meta.get("regulation_note")
    if candidate.ingredient_id is None:
        candidate.ingredient_id = meta.get("ingredient_id")


def _dedupe_resolved(candidates: list[Candidate]) -> list[Candidate]:
    """ID 매핑 후 한 번 더 합친다.

    `resolve_ingredient_ids` 가 INCI 후보의 표시명을 한글로 바꾸므로, 상담 사례에
    `헥사펩타이드-2` 와 `Hexapeptide-2` 가 함께 들어 있으면 병합 시점엔 다른 키였다가
    매핑 후 같은 성분이 된다. 그대로 두면 같은 성분이 후보 슬롯을 두 개 먹고 응답에도
    두 번 나온다.
    """
    collapsed: dict[object, Candidate] = {}
    for candidate in candidates:
        # `or` 로 쓰면 ingredient_id 가 0 인 성분이 이름 키로 새어 다른 성분과 섞인다.
        key: object = (
            candidate.name_kor if candidate.ingredient_id is None else candidate.ingredient_id
        )
        existing = collapsed.get(key)
        if existing is None:
            collapsed[key] = candidate
            continue
        existing.score = max(existing.score, candidate.score)
        for concern in candidate.concerns:
            if concern not in existing.concerns:
                existing.concerns.append(concern)
        for doc_id in candidate.source_doc_ids:
            if doc_id not in existing.source_doc_ids:
                existing.source_doc_ids.append(doc_id)
    return sorted(collapsed.values(), key=lambda c: c.score, reverse=True)


async def resolve_ingredient_ids(candidates: list[Candidate]) -> None:
    """이름만 있는 후보를 식약처 ingredients 와 잇는다. 정확 일치 → 이명 → INCI 순.

    상담 사례(`rec_cases.recommended_ingredients`)에는 한글명과 INCI 영문명이 한
    배열에 섞여 있다 — 고유 138개 중 55개(40%)가 `SULFUR` 같은 영문이다. 한글명만
    조회하면 이 40% 가 통째로 미매핑이 되어 안전성 검사를 우회하고, 사용자에게도
    영문 그대로 노출된다. `rec_efficacy.inci` 로 되짚으면 55개 전부 해소되고
    한글 표시명(`name_kor`)까지 함께 얻는다.
    """
    unresolved = [c for c in candidates if c.ingredient_id is None]
    if not unresolved:
        return
    names = [c.name_kor for c in unresolved]

    try:
        by_name = await _lookup_names(names)
    except Exception:
        logger.warning("성분 ID 조회 실패 — 미매핑으로 진행", exc_info=True)
        return  # ⑤의 명칭 보조 검사·안전성확인불가 경고로 넘어간다

    for candidate in unresolved:
        matched = by_name.get(candidate.name_kor)
        if not matched:
            continue
        candidate.ingredient_id = matched.get("ingredient_id")
        candidate.inci = candidate.inci or matched.get("name_eng") or matched.get("inci")
        # INCI 로 찾은 후보는 표시명을 한글로 바꾼다 (영문 노출 방지).
        korean = matched.get("name_kor")
        if korean:
            candidate.name_kor = normalize_ingredient_name(str(korean))


async def _lookup_names(names: list[str]) -> dict[str, dict[str, Any]]:
    """성분명 → 매칭 행. 저렴한 경로부터 시도하고 못 찾은 것만 다음 경로로 넘긴다."""
    client = await get_supabase()

    ingredient_rows = rows(
        await client.table("ingredients")
        .select("ingredient_id, name_kor, name_eng")
        .in_("name_kor", names)
        .execute()
    )
    by_name: dict[str, dict[str, Any]] = {str(row["name_kor"]): row for row in ingredient_rows}

    missing = [name for name in names if name not in by_name]
    if missing:
        synonym_rows = rows(
            await client.table("synonyms")
            .select("ingredient_id, synonym")
            .in_("synonym", missing)
            .execute()
        )
        for row in synonym_rows:
            by_name.setdefault(str(row["synonym"]), row)

    missing = [name for name in names if name not in by_name]
    if missing:
        inci_rows = rows(
            await client.table("rec_efficacy")
            .select("ingredient_id, inci, name_kor")
            .in_("inci", missing)
            .execute()
        )
        for row in inci_rows:
            by_name.setdefault(str(row["inci"]), row)

    return by_name
