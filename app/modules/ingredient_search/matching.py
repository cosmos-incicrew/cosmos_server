"""제품명 검색을 위한 결정론적 문자열 비교와 정렬."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import IntEnum

from app.modules.ingredient_search.schemas import ProductSearchCandidate

_ALNUM_PATTERN = re.compile(r"[0-9a-z가-힣]+", re.IGNORECASE)
_SCRIPT_TOKEN_PATTERN = re.compile(r"[가-힣]+|[a-z]+|\d+", re.IGNORECASE)
_BRACKET_PATTERN = re.compile(r"\[([^]]+)]")
_PARENTHESIS_PATTERN = re.compile(r"\(([^()]*)\)")
_COMBINED_CAPACITY_PATTERN = re.compile(
    r"(?<![0-9a-z가-힣])\d+(?:\.\d+)?\s*\+\s*\d+(?:\.\d+)?\s*(?:ml|g)",
    re.IGNORECASE,
)
_CAPACITY_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ml|g|매입|개입|매|개|ea)(?![0-9a-z가-힣])", re.IGNORECASE
)
_BUNDLE_PATTERN = re.compile(r"(?<![0-9a-z가-힣])\d+\s*\+\s*\d+(?!\d)")
_MARKETING_PATTERN = re.compile(
    r"(?:더블\s*)?기획|단독|추가\s*증정|증정|\bnew\b|공식|한정|올리브영|\bpick\b|단품",
    re.IGNORECASE,
)
_DEFAULT_MAXIMUM_ANCHORS = 12
_MINIMUM_ANCHOR_LENGTH = 2
_KOREAN_ANCHOR_LENGTH = 3
_OTHER_ANCHOR_LENGTH = 4


class MatchTier(IntEnum):
    EXACT_FULL_NAME = 0
    EXACT_CORE_NAME = 1
    PREFIX = 2
    SUBSTRING = 3
    ORDERED_TOKENS = 4


@dataclass(frozen=True, order=True)
class MatchKey:
    tier: MatchTier
    length_difference: int
    product_name: str
    product_id: int


def normalize_product_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(_ALNUM_PATTERN.findall(normalized))


def core_product_text(value: str) -> str:
    def clean_bracket(match: re.Match[str]) -> str:
        parts = []
        for part in re.split(r"[/,|]", match.group(1)):
            cleaned = _MARKETING_PATTERN.sub(" ", _BUNDLE_PATTERN.sub(" ", part))
            if normalize_product_text(cleaned):
                parts.append(cleaned)
        return " " + " ".join(parts) + " "

    def clean_parenthesis(match: re.Match[str]) -> str:
        content = match.group(1)
        if _CAPACITY_PATTERN.search(content) or _MARKETING_PATTERN.search(content):
            return " "
        return f" {content} "

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _BRACKET_PATTERN.sub(clean_bracket, normalized)
    normalized = _PARENTHESIS_PATTERN.sub(clean_parenthesis, normalized)
    normalized = _COMBINED_CAPACITY_PATTERN.sub(" ", normalized)
    normalized = _CAPACITY_PATTERN.sub(" ", normalized)
    normalized = _BUNDLE_PATTERN.sub(" ", normalized)
    normalized = _MARKETING_PATTERN.sub(" ", normalized)
    return normalize_product_text(normalized)


def search_anchors(query: str, maximum: int = _DEFAULT_MAXIMUM_ANCHORS) -> list[str]:
    tokens = [
        token.casefold()
        for token in _SCRIPT_TOKEN_PATTERN.findall(unicodedata.normalize("NFKC", query))
    ]
    anchors: list[str] = []
    has_word_spacing = bool(re.search(r"\s", query.strip()))
    for token in tokens:
        if len(token) >= _MINIMUM_ANCHOR_LENGTH:
            if has_word_spacing:
                anchors.append(token)
            else:
                maximum_length = (
                    _KOREAN_ANCHOR_LENGTH
                    if re.fullmatch(r"[가-힣]+", token)
                    else _OTHER_ANCHOR_LENGTH
                )
                anchors.append(token[: min(maximum_length, len(token))])
    if has_word_spacing:
        anchors.sort(key=len, reverse=True)
    return list(
        dict.fromkeys(anchor for anchor in anchors if len(anchor) >= _MINIMUM_ANCHOR_LENGTH)
    )[:maximum]


def rank_candidates(
    query: str, candidates: list[ProductSearchCandidate]
) -> list[ProductSearchCandidate]:
    query_normalized = normalize_product_text(query)
    query_tokens = [normalize_product_text(token) for token in _ALNUM_PATTERN.findall(query)]
    ranked: list[tuple[MatchKey, ProductSearchCandidate]] = []
    for candidate in candidates:
        full = normalize_product_text(candidate.product_name)
        core = core_product_text(candidate.product_name)
        tier = _match_tier(query_normalized, query_tokens, full, core)
        if tier is None:
            continue
        difference = min(
            abs(len(full) - len(query_normalized)), abs(len(core) - len(query_normalized))
        )
        ranked.append((MatchKey(tier, difference, candidate.product_name, candidate.id), candidate))
    ranked.sort(key=lambda item: item[0])
    return [candidate for _key, candidate in ranked]


def _match_tier(query: str, tokens: list[str], full: str, core: str) -> MatchTier | None:
    if query == full:
        return MatchTier.EXACT_FULL_NAME
    if query == core:
        return MatchTier.EXACT_CORE_NAME
    if full.startswith(query) or core.startswith(query):
        return MatchTier.PREFIX
    if query in full or query in core:
        return MatchTier.SUBSTRING
    meaningful_tokens = [token for token in tokens if len(token) >= _MINIMUM_ANCHOR_LENGTH]
    if meaningful_tokens and (
        _tokens_in_order(meaningful_tokens, full) or _tokens_in_order(meaningful_tokens, core)
    ):
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
