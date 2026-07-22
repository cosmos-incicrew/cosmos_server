from app.modules.ingredient_search.matching import (
    ProductMatchCandidate,
    format_tolerant_like_pattern,
    normalize_product_text,
    rank_candidates,
    requires_literal_product_lookup,
)
from app.modules.ingredient_search.schemas import ProductSearchCandidate


def test_format_tolerant_pattern_preserves_order_and_allows_spacing() -> None:
    pattern = format_tolerant_like_pattern("더샘 내추럴-마스크")

    assert pattern == "%더%샘%내%추%럴%마%스%크%"


def test_normalization_preserves_unicode_letters_and_normalizes_width() -> None:
    assert normalize_product_text("Crème 東京 化粧水 ５０ｍｌ") == "crème東京化粧水50ml"
    assert requires_literal_product_lookup("Crème 東京 化粧水") is False
    assert requires_literal_product_lookup("５０ｍｌ 크림") is True


def test_ranking_supports_non_korean_product_names() -> None:
    product = ProductSearchCandidate(
        id=1,
        product_name="[限定] 品牌 東京 化粧水 50ml",
        brand="品牌",
        main_category="스킨케어",
        sub_category=None,
        detailed_category=None,
        product_url=None,
    )
    candidate = ProductMatchCandidate(product=product, cleaned_product_name="品牌 東京 化粧水")

    assert rank_candidates("品牌 東京 化粧水", [candidate]) == [product]
