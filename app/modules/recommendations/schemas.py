"""성분 추천 요청·응답 스키마. API 계약은 docs/design/01-recommendations-pipeline.md §3.

`LlmPick`·`LlmOutput` 은 Gemini `response_schema` 로 넘겨 구조를 강제하는 **생성 전용**
모델이다. LLM 이 지어낼 수 없는 값(ingredient_id·badges·warnings·sources)은 여기에
두지 않고 코드가 ⑦에서 조립한다.
"""

from typing import Any

from pydantic import BaseModel, Field


class ChunkSource(BaseModel):
    """검색 근거의 출처. 문자열이 아니라 구조로 다룬다."""

    doc_id: str
    title: str
    locator: str | None = None  # 예: 식약처 원료 ID, PMID


class RetrievedChunk(BaseModel):
    """retrieve()의 반환 단위 — 근거 텍스트 + 0~1 정규화 score + 출처."""

    content: str
    score: float
    source: ChunkSource
    metadata: dict[str, Any]  # 컬렉션별 필요 키 (④·⑤가 소비)


class IngredientWarning(BaseModel):
    # 한도 | BSTI기피 | 알레르기유발 | 주의사항 | 안전성확인불가 | 임신수유주의 | 고민상충
    type: str
    text: str


class Source(BaseModel):
    doc_id: str
    title: str
    locator: str | None = None


class RecommendedIngredient(BaseModel):
    ingredient_id: int | None = None  # 식약처 미매핑이면 null (상세 링크 없음)
    name_kor: str
    inci: str | None = None
    concerns: list[str] = Field(default_factory=list)
    reason: str
    efficacy: str | None = None
    badges: list[str] = Field(default_factory=list)
    owned: bool = False
    owned_products: list[str] = Field(default_factory=list)
    warnings: list[IngredientWarning] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


class ContextUsed(BaseModel):
    age: int | None = None
    gender: str | None = None
    bsti_type: str | None = None
    concerns: list[str] = Field(default_factory=list)


class RecommendationResponse(BaseModel):
    status: str  # ok | insufficient_evidence
    message: str | None = None
    # 확인 불가 시 행동 유도: retry_with_other_concerns | take_bsti | retry_later
    suggested_action: str | None = None
    recommended_ingredients: list[RecommendedIngredient] = Field(default_factory=list)
    recommended_products: list[dict[str, Any]] = Field(default_factory=list)  # v1은 항상 빈 배열
    context_used: ContextUsed
    disclaimer: str


class LlmPick(BaseModel):
    """LLM 이 생성하는 것 — 성분 선택·이유·대응 고민·인용한 근거 doc_id 뿐이다."""

    name_kor: str
    reason: str
    concerns: list[str] = Field(default_factory=list)
    cited_doc_ids: list[str] = Field(default_factory=list)


class LlmOutput(BaseModel):
    picks: list[LlmPick] = Field(default_factory=list)


class UserContext(BaseModel):
    """① 컨텍스트 조립의 산출. BSTI·화장대는 없으면 빈 값으로 우아하게 축소한다."""

    user_id: str
    age: int | None = None
    gender: str | None = None
    bsti_type: str | None = None
    bsti_recommended: list[str] = Field(default_factory=list)
    bsti_caution: list[str] = Field(default_factory=list)
    owned_ingredients: list[str] = Field(default_factory=list)
    owned_products_by_ingredient: dict[str, list[str]] = Field(default_factory=dict)
    is_pregnant: bool | None = None  # None = 온보딩 미수집(unknown)
    is_nursing: bool | None = None
    concerns: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    """④ 후보 성분 집계의 단위. ⑤ 필터·⑦ 조립이 이 위에 값을 채운다."""

    name_kor: str
    score: float
    concerns: list[str] = Field(default_factory=list)
    ingredient_id: int | None = None
    inci: str | None = None
    efficacy: str | None = None
    safety_note: str | None = None
    recommended_concentration: str | None = None
    recommended_skin_types: str | None = None
    regulation_note: str | None = None
    source_doc_ids: list[str] = Field(default_factory=list)
    warnings: list[IngredientWarning] = Field(default_factory=list)
