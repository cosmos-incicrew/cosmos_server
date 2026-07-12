"""제품명·성분 이명 검색 계약. 담당: 박영기."""

from pydantic import BaseModel


class ProductSearchCandidate(BaseModel):
    product_id: str
    product_name: str
    main_category: str | None
    sub_category: str | None


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


class ProductIngredientIdsResponse(BaseModel):
    product_id: str
    product_name: str
    ingredient_ids: list[int]
    mapped_ingredient_count: int
    unmapped_ingredient_count: int
