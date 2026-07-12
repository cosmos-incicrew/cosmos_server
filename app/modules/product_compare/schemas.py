"""다중 제품 비교 요청·응답 계약. 담당: 박영기."""

from typing import Literal

from pydantic import BaseModel, Field


class ProductCompareRequest(BaseModel):
    product_ids: list[str] = Field(min_length=2)


class ComparedProduct(BaseModel):
    product_id: str
    product_name: str


class RestrictionRule(BaseModel):
    restriction_id: int
    regulate_type: str | None
    provis_atrcl: str | None
    limit_cond: str | None
    is_registered_korea: bool | None


class IngredientPresence(BaseModel):
    ingredient_id: int
    name_kr: str
    product_ids: list[str]
    presence_type: Literal["all", "partial", "single"]
    restrictions: list[RestrictionRule]


class ProductCompareResponse(BaseModel):
    products: list[ComparedProduct]
    ingredient_presence: list[IngredientPresence]
    ingredient_ids: list[int]
