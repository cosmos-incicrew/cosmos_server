from app.modules.ingredient_search.matching import core_product_text, format_tolerant_like_pattern


def test_format_tolerant_pattern_preserves_order_and_allows_spacing() -> None:
    pattern = format_tolerant_like_pattern("더샘 내추럴-마스크")

    assert pattern == "%더%샘%내%추%럴%마%스%크%"


def test_core_product_text_keeps_identifying_parenthesis_content() -> None:
    assert core_product_text("마스크 (어성초 1매 증정)") == "마스크어성초"


def test_core_product_text_does_not_remove_unwrapped_marketing_word() -> None:
    assert core_product_text("한정 에디션 크림") == "한정에디션크림"
