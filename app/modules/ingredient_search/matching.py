"""제품명 검색을 위한 결정론적 문자열 비교와 정렬."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import IntEnum

from app.modules.ingredient_search.schemas import ProductSearchCandidate

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
_MINIMUM_ANCHOR_LENGTH = 2


class MatchTier(IntEnum):
    EXACT_CLEANED_NAME = 0
    PREFIX = 1
    SUBSTRING = 2
    ORDERED_TOKENS = 3


@dataclass(frozen=True, order=True)
class MatchKey:
    tier: MatchTier
    length_difference: int
    product_name: str
    product_id: int


def normalize_product_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def format_tolerant_like_pattern(query: str) -> str:
    """정규화된 문자 순서를 유지하는 후보 조회용 ILIKE 패턴을 만든다."""

    normalized = normalize_product_text(query)
    return "%" + "%".join(normalized) + "%"


def requires_literal_product_lookup(query: str) -> bool:
    """NFKC 변환으로 원문 문자가 달라져 직접 조회가 필요한지 판정한다."""

    folded_alnum = "".join(character for character in query.casefold() if character.isalnum())
    return folded_alnum != normalize_product_text(query)


def rank_candidates(
    query: str, candidates: list[ProductSearchCandidate]
) -> list[ProductSearchCandidate]:
    query_normalized = normalize_product_text(query)
    query_tokens = [normalize_product_text(token) for token in _TOKEN_PATTERN.findall(query)]
    ranked: list[tuple[MatchKey, ProductSearchCandidate]] = []
    for candidate in candidates:
        cleaned_name = normalize_product_text(candidate.cleaned_product_name)
        tier = _match_tier(query_normalized, query_tokens, cleaned_name)
        if tier is None:
            continue
        difference = abs(len(cleaned_name) - len(query_normalized))
        ranked.append((MatchKey(tier, difference, cleaned_name, candidate.id), candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _key, candidate in ranked]


def _match_tier(query: str, tokens: list[str], cleaned_name: str) -> MatchTier | None:
    if query == cleaned_name:
        return MatchTier.EXACT_CLEANED_NAME
    if cleaned_name.startswith(query):
        return MatchTier.PREFIX
    if query in cleaned_name:
        return MatchTier.SUBSTRING
    meaningful_tokens = [token for token in tokens if len(token) >= _MINIMUM_ANCHOR_LENGTH]
    if meaningful_tokens and _tokens_in_order(meaningful_tokens, cleaned_name):
        return MatchTier.ORDERED_TOKENS
    return None


def _tokens_in_order(tokens: list[str], value: str) -> bool:
    offset = 0
    for token in tokens:
        position = value.find(token, offset)
        if position < 0:
            return False
        offset = position + len(token)
    return True
