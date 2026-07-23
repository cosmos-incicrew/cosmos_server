"""성분 추천 요청·응답 스키마. API 계약은 docs/design/01-recommendations-pipeline.md §3.

`LlmNarrative` 는 Gemini `response_schema` 로 넘겨 구조를 강제하는 **생성 전용**
모델이다. LLM 이 지어낼 수 없는 값(근거·경고·유사도)은 여기에 두지 않고 코드가
⑦에서 조립한다.
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
    # 한도 | 알레르기유발 | 주의사항 | 안전성확인불가 | 임신수유주의 | 고민상충
    type: str
    text: str


class CaseEvidence(BaseModel):
    """📂 근거 보기 — 유사 케이스 한 건 (③검색 결과에서 조립)."""

    id: str
    target_concern: str
    gender: str | None = None
    age: int | None = None
    skin_type: str | None = None
    recommended_ingredients: list[str] = Field(default_factory=list)  # 그 케이스가 추천한 성분
    similarity: float
    question: str
    answer: str  # 전문가 답변 발췌


class IngredientEvidence(BaseModel):
    """📂 근거 보기 — 성분 마스터 한 건. 성분별 주의를 여기 귀속한다."""

    name_kor: str
    inci: str | None = None
    similarity: float
    efficacy: str | None = None
    safety_note: str | None = None  # rec_efficacy 원본 서술형 주의
    concentration: str | None = None  # 권장 농도
    badges: list[str] = Field(default_factory=list)  # ⑦ 기능성 고시 배지 (미백·주름개선 등)
    owned: bool = False  # 화장대 보유 성분 여부
    owned_products: list[str] = Field(default_factory=list)  # 이 성분을 담은 보유 제품명
    warnings: list[IngredientWarning] = Field(default_factory=list)  # ⑤ 규제 경고 (이 성분)


class Answer(BaseModel):
    """①②③ 서사 — 프론트가 섹션별로 렌더한다 (마크다운 파싱 불필요)."""

    cause_analysis: str  # ① 원인 분석
    recommendation: str  # ② 추천 성분과 근거
    usage_guide: str  # ③ 사용법·관리법


class LlmNarrative(BaseModel):
    """⑥ 생성 전용 — LLM 이 만드는 것은 3단 서사와 추천 성분명뿐이다.

    recommended_names 는 ②에서 추천한 성분명 목록으로, 후보 밖 성분을 추천했는지
    검증(환각 차단)하는 데만 쓴다.
    """

    cause_analysis: str
    recommendation: str
    usage_guide: str
    recommended_names: list[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    """추천에 사용된 사용자 프로필 (구 ContextUsed). 프론트 표시·디버깅용."""

    age: int | None = None
    gender: str | None = None
    bsti_type: str | None = None
    concerns: list[str] = Field(default_factory=list)


class Advisory(BaseModel):
    """응답에 대한 알림 — 근거가 약하거나(추천은 함) 없을(추천 불가) 때만 채워진다.

    구 `low_similarity`·`message`·`suggested_action` 을 하나로 묶은 것. 알릴 게 없으면
    `RecommendationResponse.advisory` 자체가 null 이다.
    """

    code: str  # weak_evidence | no_evidence | no_candidates
    message: str  # 사용자 안내 문구
    action: str | None = None  # take_bsti | retry_with_other_concerns | retry_later


class ProductRecommendation(BaseModel):
    """🛒 추천 성분을 담은 실제 제품 (⑧ 제품 조회). LLM 미관여 — 코드가 조인·정렬한다."""

    product_id: int
    product_name: str
    brand: str | None = None
    product_url: str | None = None
    main_category: str | None = None
    matched_ingredients: list[str] = Field(default_factory=list)  # 이 제품이 담은 추천 성분명


class RecommendationResponse(BaseModel):
    status: str  # ok | insufficient_evidence
    answer: Answer | None = None  # ①②③ 서사 섹션 (확인 불가 시 null)
    cases: list[CaseEvidence] = Field(default_factory=list)
    ingredients: list[IngredientEvidence] = Field(default_factory=list)  # 성분별 경고 포함
    products: list[ProductRecommendation] = Field(default_factory=list)  # ⑧ 추천 성분 함유 제품
    advisory: Advisory | None = None  # 근거 약함/없음 알림 (없으면 null)
    retrieval_mode: str = "vector"  # 프론트 similarity 신뢰도 표시용
    user_profile: UserProfile
    disclaimer: str


class UserContext(BaseModel):
    """① 컨텍스트 조립의 산출. BSTI·화장대는 없으면 빈 값으로 우아하게 축소한다."""

    user_id: str
    age: int | None = None
    gender: str | None = None
    bsti_type: str | None = None
    bsti_recommended: list[str] = Field(default_factory=list)
    owned_ingredients: list[str] = Field(default_factory=list)
    owned_products_by_ingredient: dict[str, list[str]] = Field(default_factory=dict)
    owned_product_ids: list[int] = Field(default_factory=list)  # ⑧ 제품 추천에서 제외
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
