"""제품·성분 검색 비즈니스 로직."""

import logging

from app.common.restrictions import restriction_rules_by_ingredient
from app.modules.ingredient_search.matching import normalize_product_text
from app.modules.ingredient_search.repository import (
    IngredientSearchRepository,
    ProductSearchDataSourceError,
)
from app.modules.ingredient_search.schemas import (
    IngredientSearchResponse,
    ProductIngredientIdsResponse,
    ProductSearchResponse,
    RestrictedIngredient,
)

MIN_PRODUCT_QUERY_LENGTH = 2
MAX_PRODUCT_QUERY_LENGTH = 100
logger = logging.getLogger(__name__)


class ProductNotFoundError(Exception):
    """Raised when the selected product does not exist."""


class ProductNotAnalyzableError(Exception):
    """Raised when a product has no mapped ingredient identifiers."""


class ProductQueryTooShortError(Exception):
    """Raised when a normalized product query has fewer than two characters."""


class ProductQueryTooLongError(Exception):
    """Raised when the raw product query exceeds one hundred characters."""


class ProductSearchUnavailableError(Exception):
    """Raised when the product data source cannot complete a search."""


async def search_products(
    repository: IngredientSearchRepository, query: str, limit: int
) -> ProductSearchResponse:
    if len(query) > MAX_PRODUCT_QUERY_LENGTH:
        raise ProductQueryTooLongError
    if len(normalize_product_text(query)) < MIN_PRODUCT_QUERY_LENGTH:
        raise ProductQueryTooShortError
    try:
        results = await repository.search_products(query, limit)
    except ProductSearchDataSourceError as exc:
        logger.warning("Supabase 제품명 검색 실패", exc_info=True)
        raise ProductSearchUnavailableError from exc
    return ProductSearchResponse(query=query, results=results)


async def search_ingredients(
    repository: IngredientSearchRepository, query: str, limit: int
) -> IngredientSearchResponse:
    return IngredientSearchResponse(
        query=query,
        results=await repository.search_ingredients(query, limit),
    )


async def get_product_ingredient_ids(
    repository: IngredientSearchRepository, product_id: int
) -> ProductIngredientIdsResponse:
    product = await repository.get_product_ingredients(product_id)
    if product is None:
        raise ProductNotFoundError

    ingredient_ids: list[int] = []
    seen_ids: set[int] = set()
    unmapped_ingredient_count = 0
    for ingredient_id in product.ingredient_ids:
        if ingredient_id is None:
            unmapped_ingredient_count += 1
            continue
        if ingredient_id not in seen_ids:
            seen_ids.add(ingredient_id)
            ingredient_ids.append(ingredient_id)

    if not ingredient_ids:
        raise ProductNotAnalyzableError

    ingredient_names = await repository.get_ingredient_names(ingredient_ids)
    restrictions_by_ingredient = restriction_rules_by_ingredient(
        await repository.get_restrictions(ingredient_ids)
    )

    return ProductIngredientIdsResponse(
        id=product.id,
        product_name=product.product_name,
        ingredient_ids=ingredient_ids,
        mapped_ingredient_count=len(ingredient_ids),
        unmapped_ingredient_count=unmapped_ingredient_count,
        restricted_ingredients=[
            RestrictedIngredient(
                ingredient_id=ingredient_id,
                name_kr=ingredient_names.get(ingredient_id),
                restrictions=restrictions_by_ingredient[ingredient_id],
            )
            for ingredient_id in ingredient_ids
            if ingredient_id in restrictions_by_ingredient
        ],
    )
