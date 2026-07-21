"""다중 제품 비교 요청·응답 계약. 담당: 박영기."""

from typing import Literal

from pydantic import BaseModel, Field

from app.common.schemas import RestrictionRule

MIN_COMPARE_PRODUCT_COUNT = 2


class ProductCompareRequest(BaseModel):
    product_ids: list[int] = Field(min_length=MIN_COMPARE_PRODUCT_COUNT)


class ComparedProduct(BaseModel):
    id: int
    product_name: str


class IngredientPresence(BaseModel):
    ingredient_id: int
    name_kr: str
    product_ids: list[int]
    presence_type: Literal["all", "partial", "single"]
    restrictions: list[RestrictionRule]


class ProductCompareResponse(BaseModel):
    products: list[ComparedProduct]
    ingredient_presence: list[IngredientPresence]
    ingredient_ids: list[int]
