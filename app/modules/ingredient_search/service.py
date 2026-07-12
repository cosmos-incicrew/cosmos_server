"""제품명·성분 이명 검색 비즈니스 로직. 담당: 박영기."""

from app.modules.ingredient_search.repository import IngredientSearchRepository
from app.modules.ingredient_search.schemas import (
    IngredientSearchResponse,
    ProductIngredientIdsResponse,
    ProductSearchResponse,
)


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
    repository: IngredientSearchRepository, product_id: str
) -> ProductIngredientIdsResponse | None:
    return await repository.get_product_ingredient_ids(product_id)
