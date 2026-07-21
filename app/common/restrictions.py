"""제품 조회·비교에서 공유하는 구조화된 사용제한 규칙."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from supabase import AsyncClient

from app.common.schemas import RestrictionRule


@dataclass(frozen=True)
class RestrictionRow:
    restriction_id: int
    ingredient_id: int
    regulate_type: str | None
    provis_atrcl: str | None
    limit_cond: str | None
    is_registered_korea: bool | None


async def fetch_restriction_rows(
    client: AsyncClient, ingredient_ids: list[int]
) -> list[RestrictionRow]:
    if not ingredient_ids:
        return []
    response = await (
        client.table("restrictions")
        .select(
            "restriction_id,ingredient_id,regulate_type,provis_atrcl,limit_cond,is_registered_korea"
        )
        .in_("ingredient_id", ingredient_ids)
        .order("restriction_id")
        .execute()
    )
    results: list[RestrictionRow] = []
    for row in _rows(response.data):
        restriction_id = _integer(row.get("restriction_id"))
        ingredient_id = _integer(row.get("ingredient_id"))
        if restriction_id is None or ingredient_id is None:
            continue
        results.append(
            RestrictionRow(
                restriction_id=restriction_id,
                ingredient_id=ingredient_id,
                regulate_type=_optional_text(row.get("regulate_type")),
                provis_atrcl=_optional_text(row.get("provis_atrcl")),
                limit_cond=_optional_text(row.get("limit_cond")),
                is_registered_korea=_optional_bool(row.get("is_registered_korea")),
            )
        )
    return results


def restriction_rules_by_ingredient(
    rows: list[RestrictionRow],
) -> dict[int, list[RestrictionRule]]:
    results: dict[int, list[RestrictionRule]] = defaultdict(list)
    for row in rows:
        results[row.ingredient_id].append(
            RestrictionRule(
                restriction_id=row.restriction_id,
                regulate_type=row.regulate_type,
                provis_atrcl=row.provis_atrcl,
                limit_cond=row.limit_cond,
                is_registered_korea=row.is_registered_korea,
            )
        )
    return results


def _rows(data: Any) -> Sequence[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _integer(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
