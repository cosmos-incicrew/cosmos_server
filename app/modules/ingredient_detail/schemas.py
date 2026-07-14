"""개별 성분 해설·주의사항 입출력 모델."""

from pydantic import BaseModel


class IngredientEvidence(BaseModel):
    """해설 생성을 위한 근거 묶음. service 내부에서 조회·조립한다.

    ingredients(origin_definition) + rec_efficacy(효능·특성·안전성·출처)를 합친 것.
    """

    ingredient_id: int
    name_kr: str | None
    inci: str | None
    origin_definition: str | None = None
    efficacy: str | None = None
    product_traits: str | None = None
    recommended_skin_types: str | None = None
    properties: str | None = None
    safety_note: str | None = None
    regulation_note: str | None = None
    recommended_concentration: str | None = None
    reference_source: str | None = None

    def has_explanation_basis(self) -> bool:
        return any([self.efficacy, self.product_traits, self.origin_definition])


class IngredientDetailResponse(BaseModel):
    """개별 성분 해설·주의사항 응답."""

    status: str
    ingredient_id: int
    name: str | None = None
    body: str | None = None
    safety: str | None = None
    reference_source: str | None = None
    source_verified: bool = True
    reason: str | None = None
