"""④ 후보 성분 집계 — "찾아온 자료에서 성분 목록을 뽑는다".

검색 결과를 성분 단위로 병합·중복 제거하고, BSTI 가점과 보유 성분 하향을 적용한 뒤
식약처 `ingredients` 와 잇는다. 설계 01 §2-④.
"""

import logging
from typing import Any

from app.core.supabase import get_supabase, rows
from app.modules.recommendations import bsti_traits
from app.modules.recommendations.constants import (
    BSTI_BOOST,
    CASE_SKIN_TYPE_BOOST,
    EFFICACY_FIELDS,
    MAX_CANDIDATES,
    MIN_CANDIDATES_PER_CONCERN,
    OWNED_PENALTY,
)
from app.modules.recommendations.schemas import Candidate, RetrievedChunk, UserContext
from app.modules.recommendations.util.ingredient_names import normalize_ingredient_name

logger = logging.getLogger(__name__)

# ID 매핑에 넘길 후보 수. 중복 제거로 줄어들 몫을 감안한 여유분이며, 여기서 자르는
# 이유는 조회 비용을 묶기 위해서다. 최종 상한은 MAX_CANDIDATES 로 따로 건다.
_RESOLVE_POOL = MAX_CANDIDATES * 2


def _rank_key(candidate: Candidate) -> tuple[float, str]:
    """정렬 키. score 만 쓰면 **동점 후보의 순서가 DB 행 도착 순서로 결정된다**.

    검색 RPC 는 `order by embedding <=> query_embedding` 뿐이라(migration 015) 거리가
    같은 행의 순서가 보장되지 않는다. 파이썬 정렬은 안정 정렬이라 그 순서를 그대로
    물려받고, 실 데모의 0.71~0.72 동점권(수국잎·분홍바늘꽃·에필로비움)이 실행마다
    다른 순서로 들어와 후보 상한(MAX_CANDIDATES)에서 잘리는 성분이 바뀌었다.
    이름으로 tie-break 해 같은 검색 결과면 같은 추천이 나오게 만든다.
    """
    return (-candidate.score, candidate.name_kor)


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
    ranked = sorted(merged.values(), key=_rank_key)[:_RESOLVE_POOL]
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
    _apply_personal_weights(grounded, context, _same_skin_type_docs(case_chunks, context))
    return _reserve_concern_slots(sorted(grounded, key=_rank_key), context.concerns)


def _reserve_concern_slots(ranked: list[Candidate], concerns: list[str]) -> list[Candidate]:
    """고민당 최소 슬롯을 먼저 확보하고 남은 자리를 전역 점수순으로 채운다 (설계 04 §2-1b).

    전역 점수순만으로 자르면 검색 점수가 높은 고민이 낮은 고민을 후보 밖으로 밀어낸다.
    ⑥은 후보 목록 안에서만 추천하므로 밀려난 고민은 응답에서 통째로 사라진다.

    근거가 없는 고민(그 고민 태그가 붙은 후보가 0개)은 예약분을 쓰지 않는다 — 빈 자리를
    잡아 두면 근거 있는 고민의 성분만 줄어든다. 그 상태는 ⑩ advisory 의 partial_evidence
    가 따로 알린다.

    고민 순회 순서는 사용자가 입력한 `context.concerns` 다. `candidate.concerns` 는 검색
    결과 도착 순서로 쌓이므로 그쪽을 기준으로 돌면 예약 결과가 실행마다 흔들린다.
    """
    reserved: set[int] = set()
    for concern in concerns:
        available = [i for i, c in enumerate(ranked) if concern in c.concerns and i not in reserved]
        reserved.update(available[:MIN_CANDIDATES_PER_CONCERN])

    spare = MAX_CANDIDATES - len(reserved)
    for index in range(len(ranked)):
        if spare <= 0:
            break
        if index not in reserved:
            reserved.add(index)
            spare -= 1
    # 예약분도 전역 점수순 자리를 지킨다 — 순서를 흔들면 ⑥ 프롬프트의 상위 후보가 바뀐다.
    return [c for i, c in enumerate(ranked) if i in reserved]


def _same_skin_type_docs(case_chunks: list[RetrievedChunk], context: UserContext) -> set[str]:
    """사용자 BSTI 와 피부타입이 같은 사례의 doc_id.

    BSTI 미검사(`bsti_type=None`)이거나 사용자 축에 대응하는 사례 표기가 없으면 빈 집합이라
    가점 자체가 일어나지 않는다 — 그 사용자의 순위는 이 기능 도입 전과 같다.
    """
    skin_type = bsti_traits.case_skin_type(context.bsti_type)
    if not skin_type:
        return set()
    return {c.source.doc_id for c in case_chunks if c.metadata.get("skin_type") == skin_type}


def _apply_personal_weights(
    candidates: list[Candidate], context: UserContext, same_skin_type_docs: set[str]
) -> None:
    """BSTI 권장·같은 피부타입 사례는 올리고 이미 쓰는 성분은 내린다. dedupe 이후 1회씩.

    `base_score` 는 건드리지 않는다 — 가중치는 순위용이고 ⑩ 표시는 원점수를 써야 한다.

    피부타입 가점은 이름이 아니라 `source_doc_ids` 로 판정한다. 이름으로 보면
    `resolve_ingredient_ids` 가 한글로 바꿔 놓은 INCI 유래 후보(사례 성분의 40%)를 사례와
    다시 잇지 못한다.
    """
    bsti_recommended = set(context.bsti_recommended)
    owned = set(context.owned_ingredients)
    for candidate in candidates:
        if candidate.name_kor in bsti_recommended:
            candidate.score += BSTI_BOOST
        # BSTI_BOOST 와 겹칠 수 있다 (constants ④절) — 근거가 둘 다 맞은 성분이라 의도한 것.
        if same_skin_type_docs.intersection(candidate.source_doc_ids):
            candidate.score += CASE_SKIN_TYPE_BOOST
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
        candidate.base_score = candidate.score  # 가중치 적용 전이라 아직 같다

    concern = chunk.metadata.get("concern")
    if concern and concern not in candidate.concerns:
        candidate.concerns.append(concern)
    if chunk.source.doc_id not in candidate.source_doc_ids:
        candidate.source_doc_ids.append(chunk.source.doc_id)

    if from_efficacy:
        _fill_efficacy_fields(candidate, chunk.metadata)


def _fill_efficacy_fields(candidate: Candidate, meta: dict[str, Any]) -> None:
    """효능 인덱스에서만 오는 값들. 먼저 채워진 값을 덮어쓰지 않는다.

    ingredient_id 만 `is None` 으로 빈 값을 판정한다 — 0 이 유효한 ID 라 falsy 판정을
    쓰면 이미 매핑된 후보를 덮어쓴다.
    """
    for field in EFFICACY_FIELDS:
        current = getattr(candidate, field)
        empty = current is None if field == "ingredient_id" else not current
        if empty:
            setattr(candidate, field, meta.get(field))


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
        existing.base_score = existing.score  # 가중치 적용 전이라 아직 같다
        for concern in candidate.concerns:
            if concern not in existing.concerns:
                existing.concerns.append(concern)
        for doc_id in candidate.source_doc_ids:
            if doc_id not in existing.source_doc_ids:
                existing.source_doc_ids.append(doc_id)
    return sorted(collapsed.values(), key=_rank_key)


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


def _lowest_id_first(matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """한 조회 키에 여러 행이 걸릴 때 어느 행이 이기는지를 고정한다.

    `.in_()` 조회에는 ORDER BY 가 없어 행 순서가 보장되지 않는다. rec_efficacy 는 한
    INCI 에 여러 행이 있어(출처·농도별), 먼저 온 행이 이기면 같은 요청에도 성분 ID 와
    한글 표시명이 실행마다 달라진다.
    """
    return sorted(
        matched, key=lambda r: (r.get("ingredient_id") is None, r.get("ingredient_id") or 0)
    )


async def _lookup_names(names: list[str]) -> dict[str, dict[str, Any]]:
    """성분명 → 매칭 행. 저렴한 경로부터 시도하고 못 찾은 것만 다음 경로로 넘긴다."""
    client = await get_supabase()

    ingredient_rows = rows(
        await client.table("ingredients")
        .select("ingredient_id, name_kor, name_eng")
        .in_("name_kor", names)
        .execute()
    )
    by_name: dict[str, dict[str, Any]] = {}
    for row in _lowest_id_first(ingredient_rows):
        by_name.setdefault(str(row["name_kor"]), row)

    missing = [name for name in names if name not in by_name]
    if missing:
        synonym_rows = rows(
            await client.table("synonyms")
            .select("ingredient_id, synonym")
            .in_("synonym", missing)
            .execute()
        )
        for row in _lowest_id_first(synonym_rows):
            by_name.setdefault(str(row["synonym"]), row)

    missing = [name for name in names if name not in by_name]
    if missing:
        inci_rows = rows(
            await client.table("rec_efficacy")
            .select("ingredient_id, inci, name_kor")
            .in_("inci", missing)
            .execute()
        )
        for row in _lowest_id_first(inci_rows):
            by_name.setdefault(str(row["inci"]), row)

    return by_name
