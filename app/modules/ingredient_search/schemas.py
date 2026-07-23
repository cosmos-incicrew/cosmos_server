"""제품·성분 검색 API 계약."""

from pydantic import BaseModel

from app.common.schemas import RestrictionRule


class ProductSearchCandidate(BaseModel):
    id: int
    cleaned_product_name: str
    brand: str | None
    main_category: str | None
    sub_category: str | None
    detailed_category: str | None
    product_url: str | None


class ProductSearchResponse(BaseModel):
    query: str
    results: list[ProductSearchCandidate]


class IngredientSearchCandidate(BaseModel):
    ingredient_id: int
    name_kr: str
    name_en: str | None


class IngredientSearchResponse(BaseModel):
    query: str
    results: list[IngredientSearchCandidate]


class RestrictedIngredient(BaseModel):
    ingredient_id: int
    name_kr: str | None
    restrictions: list[RestrictionRule]


class ProductIngredientIdsResponse(BaseModel):
    id: int
    product_name: str
    ingredient_ids: list[int]
    mapped_ingredient_count: int
    unmapped_ingredient_count: int
    restricted_ingredients: list[RestrictedIngredient]
