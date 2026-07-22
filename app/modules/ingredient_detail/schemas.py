"""개별 성분 해설·주의사항 입출력 모델."""

from pydantic import BaseModel


class Restriction(BaseModel):
    """식약처 등 공식 규제 정보 (restrictions 테이블 1행)."""

    restriction_id: int | None = None
    regulate_type: str | None = None  # 제한·금지 등 규제 유형
    notice_ingr_name: str | None = None  # 고시 성분명
    provis_atrcl: str | None = None  # 단서·예외 조항
    limit_cond: str | None = None  # 사용 한도·조건
    is_registered_korea: bool | None = None  # 국내 등록 여부

    def has_content(self) -> bool:
        return any([self.regulate_type, self.provis_atrcl, self.limit_cond])


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
    restrictions: list[Restriction] = []

    def has_explanation_basis(self) -> bool:
        return any([self.efficacy, self.product_traits, self.origin_definition])

    def has_safety_basis(self) -> bool:
        """주의사항 근거 유무. 공식 규제 또는 안전성 참고 문구 중 하나라도 있으면 True."""
        return bool(self.safety_note) or any(r.has_content() for r in self.restrictions)


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


class ProductSummaryRequest(BaseModel):
    """제품 요약 요청. 프론트가 제품 전성분 id를 배합순으로 전달."""

    ingredient_ids: list[int]


class TopIngredient(BaseModel):
    """대표 성분(배합순 상위)."""

    ingredient_id: int
    name: str | None = None


class ProductSummaryResponse(BaseModel):
    """제품 요약 응답. 대표성분 + 제품 해설 요약."""

    status: str
    top_ingredients: list[TopIngredient] = []
    summary: str | None = None
    source_verified: bool = True
    reason: str | None = None


# ── 다중 제품 비교 해설 ────────────────────────────────────────
# 영기님 POST /api/v1/products/compare 응답을 프론트가 그대로 전달한다.


class ComparedProduct(BaseModel):
    """비교 대상 제품."""

    id: int
    product_name: str | None = None


class IngredientPresence(BaseModel):
    """성분별 제품 포함 관계 + 구조화된 주의사항."""

    ingredient_id: int
    name_kr: str | None = None
    product_ids: list[int] = []
    presence_type: str | None = None  # all | partial | single
    restrictions: list[Restriction] = []


class ComparisonSummaryRequest(BaseModel):
    """비교 해설 요청. 검색엔진 compare 응답을 그대로 전달받는다."""

    products: list[ComparedProduct]
    ingredient_presence: list[IngredientPresence]


class ComparisonSummaryResponse(BaseModel):
    """비교 해설 응답."""

    status: str
    summary: str | None = None
    source_verified: bool = True
    reason: str | None = None


# ── 성분 이름 조회 ────────────────────────────────────────────
# 프론트가 성분 id 목록만 가지고 있을 때 화면에 이름을 표시하기 위해 사용한다.


class IngredientNameRequest(BaseModel):
    """성분 이름 조회 요청."""

    ingredient_ids: list[int]


class IngredientName(BaseModel):
    """성분 id와 이름."""

    ingredient_id: int
    name_kr: str | None = None
    name_en: str | None = None


class IngredientNameResponse(BaseModel):
    """성분 이름 조회 응답. 요청한 순서를 유지한다."""

    ingredients: list[IngredientName] = []
