"""다중 제품 성분 비교 비즈니스 로직. 담당: 박영기."""

from collections import defaultdict

from app.common.restrictions import restriction_rules_by_ingredient
from app.modules.product_compare.repository import (
    ProductCompareRepository,
)
from app.modules.product_compare.schemas import (
    ComparedProduct,
    IngredientPresence,
    ProductCompareResponse,
)


class DuplicateProductIdsError(Exception):
    """Raised when a comparison request contains the same product twice."""


class ProductCompareLimitExceededError(Exception):
    """Raised when a comparison request exceeds the configured product limit."""


class ProductNotFoundError(Exception):
    """Raised when one or more requested products do not exist."""


class ProductNotAnalyzableError(Exception):
    """Raised when a requested product has no mapped ingredient identifiers."""


async def compare_products(
    repository: ProductCompareRepository, product_ids: list[int], max_product_count: int
) -> ProductCompareResponse:
    if len(set(product_ids)) != len(product_ids):
        raise DuplicateProductIdsError
    if len(product_ids) > max_product_count:
        raise ProductCompareLimitExceededError

    products = await repository.get_products(product_ids)
    if len(products) != len(product_ids):
        raise ProductNotFoundError

    ingredient_ids_by_product: dict[int, list[int]] = {}
    all_ingredient_ids: list[int] = []
    seen_ingredient_ids: set[int] = set()
    for product in products:
        unique_ingredient_ids = _ordered_unique(product.ingredient_ids)
        if not unique_ingredient_ids:
            raise ProductNotAnalyzableError
        ingredient_ids_by_product[product.id] = unique_ingredient_ids
        for ingredient_id in unique_ingredient_ids:
            if ingredient_id not in seen_ingredient_ids:
                seen_ingredient_ids.add(ingredient_id)
                all_ingredient_ids.append(ingredient_id)

    ingredient_names = await repository.get_ingredient_names(all_ingredient_ids)
    restrictions_by_ingredient = restriction_rules_by_ingredient(
        await repository.get_restrictions(all_ingredient_ids)
    )
    product_ids_by_ingredient: dict[int, list[int]] = defaultdict(list)
    for product_id, ingredient_ids in ingredient_ids_by_product.items():
        for ingredient_id in ingredient_ids:
            product_ids_by_ingredient[ingredient_id].append(product_id)

    total_product_count = len(products)
    return ProductCompareResponse(
        products=[
            ComparedProduct(id=product.id, product_name=product.product_name)
            for product in products
        ],
        ingredient_presence=[
            IngredientPresence(
                ingredient_id=ingredient_id,
                name_kr=ingredient_names[ingredient_id],
                product_ids=product_ids_by_ingredient[ingredient_id],
                presence_type=_presence_type(
                    len(product_ids_by_ingredient[ingredient_id]), total_product_count
                ),
                restrictions=restrictions_by_ingredient[ingredient_id],
            )
            for ingredient_id in all_ingredient_ids
        ],
        ingredient_ids=all_ingredient_ids,
    )


def _ordered_unique(ingredient_ids: list[int | None]) -> list[int]:
    results: list[int] = []
    seen_ids: set[int] = set()
    for ingredient_id in ingredient_ids:
        if ingredient_id is not None and ingredient_id not in seen_ids:
            seen_ids.add(ingredient_id)
            results.append(ingredient_id)
    return results


def _presence_type(product_count: int, total_product_count: int) -> str:
    if product_count == total_product_count:
        return "all"
    if product_count == 1:
        return "single"
    return "partial"
