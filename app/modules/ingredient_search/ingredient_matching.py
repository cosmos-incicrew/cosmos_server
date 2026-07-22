"""성분 표준명과 이명을 위한 결정론적 문자열 비교와 정렬."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import IntEnum


class IngredientMatchTier(IntEnum):
    EXACT_STANDARD_NAME = 0
    EXACT_SYNONYM = 1
    PREFIX_STANDARD_NAME = 2
    PREFIX_SYNONYM = 3
    SUBSTRING_STANDARD_NAME = 4
    SUBSTRING_SYNONYM = 5


@dataclass(frozen=True)
class IngredientMatchCandidate:
    ingredient_id: int
    name_kor: str
    name_eng: str | None
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True, order=True)
class IngredientMatchKey:
    tier: IngredientMatchTier
    length_difference: int
    name_kor: str
    ingredient_id: int


def normalize_ingredient_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def ingredient_like_pattern(query: str) -> str:
    """공백·구두점 차이를 허용하면서 문자 순서는 유지하는 ILIKE 패턴."""

    normalized = normalize_ingredient_text(query)
    return "%" + "%".join(normalized) + "%"


def rank_ingredient_candidates(
    query: str, candidates: list[IngredientMatchCandidate]
) -> list[IngredientMatchCandidate]:
    normalized_query = normalize_ingredient_text(query)
    if not normalized_query:
        return []

    ranked: list[tuple[IngredientMatchKey, IngredientMatchCandidate]] = []
    for candidate in candidates:
        key = _match_key(normalized_query, candidate)
        if key is not None:
            ranked.append((key, candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _key, candidate in ranked]


def _match_key(
    query: str, candidate: IngredientMatchCandidate
) -> IngredientMatchKey | None:
    standard_names = _normalized_values((candidate.name_kor, candidate.name_eng))
    synonyms = _normalized_values(candidate.synonyms)
    match_groups = (
        (IngredientMatchTier.EXACT_STANDARD_NAME, standard_names, _is_exact),
        (IngredientMatchTier.EXACT_SYNONYM, synonyms, _is_exact),
        (IngredientMatchTier.PREFIX_STANDARD_NAME, standard_names, _is_prefix),
        (IngredientMatchTier.PREFIX_SYNONYM, synonyms, _is_prefix),
        (IngredientMatchTier.SUBSTRING_STANDARD_NAME, standard_names, _is_substring),
        (IngredientMatchTier.SUBSTRING_SYNONYM, synonyms, _is_substring),
    )
    for tier, values, predicate in match_groups:
        differences = [abs(len(value) - len(query)) for value in values if predicate(query, value)]
        if differences:
            return IngredientMatchKey(
                tier=tier,
                length_difference=min(differences),
                name_kor=candidate.name_kor,
                ingredient_id=candidate.ingredient_id,
            )
    return None


def _normalized_values(values: tuple[str | None, ...] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        normalized
        for value in values
        if value and (normalized := normalize_ingredient_text(value))
    )


def _is_exact(query: str, value: str) -> bool:
    return query == value


def _is_prefix(query: str, value: str) -> bool:
    return value.startswith(query) and query != value


def _is_substring(query: str, value: str) -> bool:
    return query in value and not value.startswith(query)
