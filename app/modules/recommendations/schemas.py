"""성분 추천 요청·응답 스키마. API 계약은 docs/design/01-recommendations-pipeline.md §3.

`LlmNarrative` 는 Gemini `response_schema` 로 넘겨 구조를 강제하는 **생성 전용**
모델이다. LLM 이 지어낼 수 없는 값(근거·경고·유사도)은 여기에 두지 않고 코드가
⑩에서 조립한다.
"""

from typing import Any

from pydantic import BaseModel, Field, model_validator


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
    # 검색 코사인 유사도. **null = 유사도 없음 = 표(BSTI) 기반 추천** — 검색으로 얻은
    # 값이 아니라서 비운다. 예전엔 표 매칭에 코드가 준 상수 1.0 이 실려, 프론트가 이 값을
    # 신뢰도로 표시하면 BSTI 성분이 고민 성분(0.7~0.85)보다 정확해 보였다.
    similarity: float | None = None
    efficacy: str | None = None
    safety_note: str | None = None  # rec_efficacy 원본 서술형 주의
    concentration: str | None = None  # 권장 농도
    badges: list[str] = Field(default_factory=list)  # ⑩ 기능성 고시 배지 (미백·주름개선 등)
    owned: bool = False  # 화장대 보유 성분 여부
    owned_products: list[str] = Field(default_factory=list)  # 이 성분을 담은 보유 제품명
    warnings: list[IngredientWarning] = Field(default_factory=list)  # ⑤ 규제 경고 (이 성분)
    # both(고민+타입) | concern(고민) | bsti(타입).
    # 프론트가 "고민·타입 모두 적합" 배지로 근거를 구분해 보여주는 데 쓴다.
    match_source: str | None = None


class Answer(BaseModel):
    """①②③ 서사 — 프론트가 섹션별로 렌더한다 (마크다운 파싱 불필요)."""

    cause_analysis: str  # ① 원인 분석
    recommendation: str  # ② 추천 성분과 근거
    usage_guide: str  # ③ 사용법·관리법


class TranslatedIngredientText(BaseModel):
    """⑥이 번역한 영어 **조각**들. 성분명으로 후보와 다시 잇는다.

    임의 키 dict(`{성분명: 번역}`)가 아니라 객체 리스트인 이유는 Gemini
    `response_schema` 가 고정 키 스키마만 다루기 때문이다.

    값이 문자열이 아니라 리스트인 이유는 원문에 한국어와 영어가 섞여 있기 때문이다
    (레조시놀 `safety_note`). 통째로 번역시키면 한국어 안전 문구까지 LLM 이 다시 쓰므로,
    영어 조각만 번호 순서대로 받아 ⑩이 제자리에 끼워 넣는다. 순서와 개수가 곧 자리
    정보라, 하나라도 빠지면 ⑩이 전량 폐기한다 (`text.apply_translations`).
    """

    # 필드 설명은 Gemini `response_schema` 로 함께 넘어간다. 지시문에만 적었더니 모델이
    # 영어 원문을 그대로 복사해 돌려줬다 — 값의 언어를 스키마 쪽에도 못박는다.
    name_kor: str = Field(description="번역 대상 목록에 적힌 성분명 그대로")
    efficacy: list[str] = Field(
        default_factory=list,
        description="efficacy 의 영어 조각을 번호 순서대로 옮긴 **한국어** 문장들",
    )
    safety_note: list[str] = Field(
        default_factory=list,
        description="safety_note 의 영어 조각을 번호 순서대로 옮긴 **한국어** 문장들",
    )


class LlmNarrative(BaseModel):
    """⑥ 생성 전용 — LLM 이 만드는 것은 3단 서사와 추천 성분명, 영어 원문 번역뿐이다.

    recommended_names 는 ②에서 추천한 성분명 목록으로, 후보 밖 성분을 추천했는지
    검증(환각 차단)하는 데만 쓴다.

    translations 는 `rec_efficacy` 원문의 영어 조각을 한국어로 옮긴 것이다. DB 에 번역본을
    저장하지 않고 생성 호출에 얹는 방식이라, 실패해도 ⑩이 `clean_display_text` 로
    떨어져 파이프라인이 죽지 않는다.
    """

    cause_analysis: str
    recommendation: str
    usage_guide: str
    recommended_names: list[str] = Field(default_factory=list)
    translations: list[TranslatedIngredientText] = Field(default_factory=list)


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

    code: str  # weak_evidence | partial_evidence | no_evidence | no_candidates
    message: str  # 사용자 안내 문구
    action: str | None = None  # take_bsti | retry_with_other_concerns | retry_later


class ProductRecommendation(BaseModel):
    """🛒 추천 성분을 담은 실제 제품 (⑨ 제품 조회). LLM 미관여 — 코드가 조인·정렬한다."""

    product_id: int
    product_name: str
    brand: str | None = None
    product_url: str | None = None
    main_category: str | None = None
    matched_ingredients: list[str] = Field(default_factory=list)  # 이 제품이 담은 추천 성분명
    # 담은 추천 성분의 출처 — 성분 카드(`IngredientEvidence.match_source`)와 같은 어휘라
    # 프론트가 배지 매핑을 한 벌만 유지한다. 매칭 출처가 없으면 null.
    match_source: str | None = None


class RecommendationResponse(BaseModel):
    """필드 순서가 곧 프론트가 읽는 JSON 순서다 (노션 응답 계약).

    ⑧ 종합(`top_*`)이 메인 카드이고 `cases` 는 "왜 이게 뽑혔나"의 상세 근거다.
    고민 축·BSTI 축을 따로 싣던 배열 4종은 ⑧이 두 축을 대표로 합치면서 걷어냈다
    (2026-07-23) — 축 구분은 배열이 아니라 `match_source` 로 전달한다.
    """

    status: str  # ok | insufficient_evidence
    answer: Answer | None = None  # ①②③ 서사 섹션 (확인 불가 시 null)
    cases: list[CaseEvidence] = Field(default_factory=list)
    top_ingredients: list[IngredientEvidence] = Field(default_factory=list)
    top_products: list[ProductRecommendation] = Field(default_factory=list)
    advisory: Advisory | None = None  # 근거 약함/없음 알림 (없으면 null)
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
    owned_product_ids: list[int] = Field(default_factory=list)  # ⑨ 제품 추천에서 제외
    is_pregnant: bool | None = None  # None = 온보딩 미수집(unknown)
    is_nursing: bool | None = None
    concerns: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    """④ 후보 성분 집계의 단위. ⑤ 필터·⑩ 조립이 이 위에 값을 채운다."""

    name_kor: str
    # 정렬용 내부 점수 — ④가 BSTI 가점·보유 하향을 더해 1.0 을 넘길 수 있다.
    score: float
    # 표시용 원점수(코사인 유사도 0~1). 가중치를 섞으면 ⑩의 similarity 가 1 을 넘고,
    # 같은 성분이 `ingredients[]`(청크 원점수)와 다른 값으로 실려 한 응답에 두 값이 된다.
    # 기본값은 아래 검증기가 score 로 채우므로 0.0 이 그대로 남지는 않는다.
    base_score: float = 0.0

    # 이 후보가 어느 고민에서 회수됐는지. ④가 채우고 ④의 고민당 슬롯 예약과
    # ⑥ 프롬프트의 고민 라벨이 읽는다 (설계 04 §2-1(b)).
    concerns: list[str] = Field(default_factory=list)
    ingredient_id: int | None = None
    inci: str | None = None
    product_traits: str | None = None  # ⑩ 이 efficacy 앞에 잇는 선행 서술
    efficacy: str | None = None
    safety_note: str | None = None
    recommended_concentration: str | None = None
    recommended_skin_types: str | None = None
    regulation_note: str | None = None
    source_doc_ids: list[str] = Field(default_factory=list)
    warnings: list[IngredientWarning] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _default_base_score(cls, data: Any) -> Any:
        """base_score 미지정이면 score 와 같다 — 가중치 적용 전이 곧 원점수다."""
        if isinstance(data, dict) and data.get("base_score") is None:
            return {**data, "base_score": data.get("score")}
        return data
