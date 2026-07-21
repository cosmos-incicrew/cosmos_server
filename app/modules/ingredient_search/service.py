"""제품·성분 검색 비즈니스 로직."""

from app.common.restrictions import restriction_rules_by_ingredient
from app.modules.ingredient_search.repository import IngredientSearchRepository
from app.modules.ingredient_search.schemas import (
    IngredientSearchResponse,
    ProductIngredientIdsResponse,
    ProductSearchResponse,
    RestrictedIngredient,
)


class ProductNotFoundError(Exception):
    """Raised when the selected product does not exist."""


class ProductNotAnalyzableError(Exception):
    """Raised when a product has no mapped ingredient identifiers."""


async def search_products(
    repository: IngredientSearchRepository, query: str, limit: int
) -> ProductSearchResponse:
    return ProductSearchResponse(
        query=query,
        results=await repository.search_products(query, limit),
    )


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
