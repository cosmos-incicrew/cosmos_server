from app.modules.ingredient_search.matching import (
    core_product_text,
    format_tolerant_like_pattern,
    normalize_product_text,
    rank_candidates,
    requires_literal_product_lookup,
)
from app.modules.ingredient_search.schemas import ProductSearchCandidate


def test_format_tolerant_pattern_preserves_order_and_allows_spacing() -> None:
    pattern = format_tolerant_like_pattern("더샘 내추럴-마스크")

    assert pattern == "%더%샘%내%추%럴%마%스%크%"


def test_core_product_text_keeps_identifying_parenthesis_content() -> None:
    assert core_product_text("마스크 (어성초 1매 증정)") == "마스크어성초"


def test_core_product_text_does_not_remove_unwrapped_marketing_word() -> None:
    assert core_product_text("한정 에디션 크림") == "한정에디션크림"


def test_normalization_preserves_unicode_letters_and_normalizes_width() -> None:
    assert normalize_product_text("Crème 東京 化粧水 ５０ｍｌ") == "crème東京化粧水50ml"
    assert requires_literal_product_lookup("Crème 東京 化粧水") is False
    assert requires_literal_product_lookup("５０ｍｌ 크림") is True


def test_ranking_supports_non_korean_product_names() -> None:
    candidate = ProductSearchCandidate(
        id=1,
        product_name="品牌 東京 化粧水 50ml",
        brand="品牌",
        main_category="스킨케어",
        sub_category=None,
        detailed_category=None,
        product_url=None,
    )

    assert rank_candidates("品牌 東京 化粧水", [candidate]) == [candidate]
