# 성분 추천(recommendations) 파이프라인 설계서

- **작성일**: 2026-07-10
- **작성자**: 김민경
- **대상 모듈**: `app/modules/recommendations` (담당: 김민경) · 엔드포인트: `POST /api/v1/recommendations`
- **네이밍**: API 경로(`/recommendations`)에 맞춰 모듈·문서·태그를 recommendations(복수)로 통일 (패키지 개칭 완료)

사용자의 온보딩 프로필(나이·성별·피부 고민)·BSTI 검사 결과·**화장대에 등록한 보유
제품의 성분**을 입력으로, AI Hub 스킨케어 성분-효능 데이터(dataSetSn=71886) 기반
RAG로 개인 맞춤 성분을 추천한다. cosmos의 흐름(제품 검색 → 전성분 해설 → 교차 분석
→ 추천)에서 추천은 **분석 보고서의 결론부**다 — 사용자의 현재 루틴을 알아야 "부족한
성분"을 말할 수 있다. 데이터 테이블·적재·임베딩 명세는
[02-recommendations-data-spec.md](02-recommendations-data-spec.md) 참조.

## 0-1. 구현 반영 노트 (2026-07-20)

구현하면서 **실제 DB가 초기 설계 전제와 달랐다.** 본문(§0~§7)은 아래 실제 구조를 반영해
갱신됐다(2026-07-22) — **본문이 현재 진실이다.** 이 표는 무엇이 왜 바뀌었는지의 변경
이력으로 남긴다.

| 이 문서의 전제 | 실제 (2026-07-20 기준) |
|---|---|
| `bsti_results(type_code, recommended_ingredients, caution_ingredients)` | **그런 테이블은 없다.** 박금별 실제 스키마는 `bsti_user_diagnoses`(user_id·result_code) + `bsti_type_ingredients`(type_code·relation `recommend`/`avoid`) + `bsti_ingredients`(name_ko) 3테이블 조인. 게다가 아직 `origin/bsti-table` 미머지라 DB에 없다. **2026-07-22 최종: 박금별 DB 매핑 대신 코드 상수 `BSTI_RECOMMENDED`(앱 `kBstiSkinTypes` 복제본) 채택**, 없으면 BSTI 요소만 생략 |
| `user_shelf(item_type, ref_id)` | 테이블 자체가 없다. 코드는 `item_type`·`product_id`·`ingredient_name`을 가정한다 — **박금별과 확정 필요** |
| `ingredients.name_kr` / `.id` | 실제 컬럼은 `name_kor` / `ingredient_id` (`name_eng`도 마찬가지) |
| §5 "`retrieve()`가 목 스텁(고정 청크)" | 목이 아니라 **실 DB 키워드 검색**(컬럼 필터·ILIKE)으로 구현. 벡터 검색만 미도입 |
| 공유 `retrieve()` 유틸 (`app/common/retrieval.py`) | 이호영이 자체 조회를 쓰기로 해 **공유 폐기**. `app/modules/recommendations/pipeline/s3_retrieval.py`로 이동 |
| §2-⑦ "배지·**고시 함량** 부착" | §3 API 계약에 함량 필드가 없어 문서 내부 모순. **함량 제외**로 통일 |
| §3 `inci`·`efficacy` 타입 `string` | 미매핑 성분이 있어 `str \| None`이 맞다 |

**구현에서 추가로 알게 된 데이터 사실** (설계 시점엔 몰랐던 것):

- `rec_efficacy.name_kr` 2,270행 중 개행 12·괄호 15건 (`"레티놀\n(비타민 A)"`). 정규화 없이
  상수를 조회하면 임신 금기 필터가 통째로 빗나간다 → `normalize_ingredient_name()` 도입
- `rec_cases.recommended_ingredients`의 고유 성분명 138개 중 55개가 INCI 영문.
  `rec_efficacy.inci`로 역매핑하면 전부 해소된다

**변경 이력**:

- 2026-07-22: **⑧ 제품 추천 추가** (`s8_products.py`). 추천 성분→`product_ingredients`
  역조회로 함유 제품을 커버리지 순 추천. 응답에 `products[]` 신설, `UserContext`에
  `owned_product_ids` 추가 (보유 제품 제외용). §0-4·§2-⑧ 참조.
- 2026-07-22: **4관점(개발·QA·보안·DBA) 검토 반영**:
  - `IngredientEvidence`에 `badges`(기능성 고시)·`owned`·`owned_products` 배선 (문서엔
    있으나 미구현이던 기능 완성). ⑦이 정규화 키로 조회해 더러운 이름의 경고 소실 수정.
  - ⑥ 재생성 가드를 모든 시도에 적용, Langfuse 장애 격리. `get_supabase` 동시 생성 락.
  - 엔드포인트 **rate limit**(`rate_limit.py`, 사용자당 {RATE_LIMIT_MAX}/{window}s) → 429.
  - 마이그레이션 001 드리프트 수정(`name_kor`/`name_eng`·vector 확장 순서·`origin_ingredients`).
  - 미해결(외부 의존): **③ 벡터 검색**(서지우 RPC) — 키워드 모드는 관련성 순위가 없어
    추천 품질의 구조적 상한. score 컷도 가짜 하한(0.55) > 임계값(0.5)이라 무효.
- 2026-07-22 (2차 재검토): **회귀 2건 수정**:
  - ⑦ 근거 패널(`ingredients[]`)이 ③ 원본(efficacy_chunks)을 순회해 **⑤가 제거한
    성분(임신 금기 레티놀·금지 성분)이 배지 달고 무경고로 재노출**되던 것 차단 —
    `safe` 후보에 있는 성분만·정규화 이름 1회만 (`_safe_ingredients`). `cases[]`도 중복 제거.
  - `rate_limit` 스레드 경쟁(동기 def 의존성=스레드풀 실행) → `threading.Lock`.
  - s8 역조회 상한(`PRODUCT_FETCH_LIMIT=500`), 005 FK `bigint` 정합, Langfuse output 개인속성 마스킹.
  - 잔여(외부/결정 필요): `ingredient_search/repository.py`의 구 컬럼명(`name_kr`) 드리프트(영기
    모듈), IP·전역 rate limit(다계정 우회), 품질 평가(recall/groundedness) 게이트, user_shelf 미구현.

## 0-2. 파일 구성 (2026-07-20 리팩터링)

`service.py` 는 순서만 다루는 오케스트레이터이고, 단계 구현은 `pipeline/` 하위에 있다.
파일명 앞의 `s1`~`s7` 이 실행 순서다 — 파이썬 모듈명은 숫자로 시작할 수 없어 `01_` 대신
`s1_` 을 쓴다.

```
app/modules/recommendations/
├── service.py            오케스트레이터 (순서·조기 종료만)
├── router.py             엔드포인트
├── pipeline/
│   ├── s1_context.py     ① 컨텍스트 조립
│   ├── s2_queries.py     ② 질의 구성
│   ├── s3_retrieval.py   ③ 검색
│   ├── s4_candidates.py  ④ 후보 집계
│   ├── s5_safety.py      ⑤ 안전성 필터
│   ├── s6_generation.py  ⑥ 생성
│   └── s7_response.py    ⑦ 응답 조립
├── schemas.py            요청·응답·내부 모델
├── constants.py          임계값·가중치·안전성 참조 목록
├── prompts.py            ⑥ 생성 프롬프트 (llm-rag-rules 규약상 모듈 루트)
├── names.py              성분명 정규화 (④⑤⑦ 공용)
├── errors.py             409·502·503 에러 계약
└── bsti_traits.py        BSTI 타입 코드 → 피부 특성 (②⑤ 공용)
```

단계끼리 직접 부르지 않는다. 예외는 ⑦이 ⑥의 금칙어 순화를 재사용하는 것 하나뿐이다
(같은 화장품법 표시·광고 규칙이라 두 벌로 두면 어긋난다).
## 0-3. 임신·수유 주의 성분 근거 (2026-07-20 조사)

**국내 규제 근거는 없다.** 「화장품 안전기준 등에 관한 규정」(식약처고시 제2026-19호)
본문 전수 확인 결과 원료 규제 축은 제3조 사용할 수 없는 원료(별표 1)·제4조 사용상의
제한이 필요한 원료(별표 2) 둘뿐이고 **사용자 집단(임부·수유부)별 축이 없다**. 아래는
해외 근거를 참고한 자체 판단이며, 사용자에게 "식약처 기준"으로 표시하면 안 된다.

| 단계 | 성분 | 근거 | 동작 |
|---|---|---|---|
| 제외 | 레티노이드 (레티놀·레티닐에스터류·레틴아마이드류) | 국소 노출에서 **위험 증가 미확인** — PMID 22174426 (235 vs 444, 주요 기형 OR 1.8, CI 0.6–5.4) · PMID 15940677 (106 vs 389, 2.2% vs 1.2%, P=0.62). 그럼에도 저자 결론이 *"topical retinoids cannot be advised for use during pregnancy"* | 임신·수유 확인 시 제거. 사유는 "금기"가 아니라 **"권고되지 않음"** |
| 경고 | 살리실릭애씨드 | EU CLP Repr. 2 / H361d 분류(hazard)이나, 같은 SCCS 의견서가 농도 조건부 허용(씻어내는 두발 3.0%·기타 2.0%·아이메이크업 등 0.5%)하며 **임부 제한을 부과하지 않음**. 발달독성 근거는 경구 아스피린 동물실험. MotherSafe(NSW Health)는 *"considered safe to use in pregnancy"* | 제거하지 않고 함량 확인 안내 |

우리 데이터에 제품 내 농도 정보가 없어 농도 기반 판단은 불가능하다 — 이것이 살리실릭
애씨드를 제외가 아닌 경고로 두는 실무적 이유다.

**미확인**: 별표 1·2 원문(첨부파일)은 확인하지 못했다. 서지우가 `restrictions`(식약처
오픈API 15111772)를 적재하면 데이터로 자동 확인된다. 정유(에센셜 오일) 계열은 조사
범위 밖. 출시 전 전문가 검토 권장.


## 0-4. 목표 검색 설계 — 벡터 (2026-07-22 확정)

**이 다이어그램은 ③ 검색의 목표(벡터) 설계다. 현재 코드 ③은 아직 벡터가 아니라
ILIKE 키워드·배열필터 플레이스홀더다(§0-1 참조).** 서지우의 검색 RPC가 붙는 시점의
목적지이며, 파란 노드(임베딩 유사도)가 실제 코드로 채워질 자리다.

확정 사항:
- **두 다리를 모두 벡터로, 병렬 유지.** cases 다리(유사 사례 → 85개 고신뢰 성분) +
  efficacy 다리(성분 사전 → 2,270개 넓은 커버리지)를 s4에서 병합. 양자택일이 아니다.
- **efficacy 임베딩은 fallback이 아니라 상시 가동** — 85개를 넘어 2,270개에서 추천하려는
  목적. 대신 §2-⑤ 안전 필터의 책임이 커진다.
- **질의 텍스트 출처**: cases 다리는 사람묘사+고민 문장, efficacy 다리는 고민 사전
  (`CONCERN_SEARCH_KEYWORDS`, constants.py에서 사람이 관리)에서 파생한 짧은 구절.
  효능 텍스트는 질의가 아니라 코퍼스(`rec_efficacy.efficacy`) 쪽에 있다.
- **임베딩 프롬프트**: 코퍼스는 오프라인 인덱싱 때 passage 접두어, 질의는 검색 때 query
  접두어 (e5 `query:`/`passage:`, jina `task` 파라미터, bge·gemini 불필요).

```mermaid
flowchart TD
    subgraph OFF["⏱ 오프라인 인덱싱 (미리 1회) — passage 프롬프트"]
        OC1["rec_cases.question 텍스트"] -->|"passage 접두어<br/>e5:'passage:' · jina:retrieval.passage"| OE1["임베딩"] --> OV1[("rec_cases<br/>임베딩 저장")]
        OC2["rec_efficacy.efficacy 텍스트<br/>(성분별 효능 설명 · 2270행)"] -->|"passage 접두어"| OE2["임베딩"] --> OV2[("rec_efficacy<br/>임베딩 저장")]
    end

    U["요청 user_id"] --> S1["① s1_context<br/>프로필·BSTI·화장대"]
    S1 --> S2["② s2_queries<br/>고민별 검색어 N개 생성"]

    DICT["📖 고민 사전 (constants.py, 사람이 관리)<br/>CONCERN_SEARCH_KEYWORDS<br/>redness→'홍조 진정' · dryness→'건조 보습 각질'"]
    DICT -.->|"고민코드 → 질의 텍스트"| S2

    S2 --> FAN{{"③ s3_retrieval<br/>asyncio.gather 팬아웃<br/>고민 N개 × 2다리 = 2N 병렬"}}

    FAN --> RC1["고민1"]
    FAN --> RC2["고민2"]
    FAN --> RCN["고민N …"]

    subgraph ONE["고민 1개 처리 — 두 다리 병렬 (내부 asyncio.gather)"]
        direction LR
        subgraph LC["cases 다리 — 유사 사례"]
            LCQ["질의: 사람묘사+고민<br/>'30대 여성 건성 홍조...'"] -->|"query 접두어"| LCE["질의 임베딩"]
            LCE -->|"코사인 유사도"| LCS[("rec_cases<br/>question 임베딩")]
            LCS --> LCT["유사 케이스 top-K<br/>→ recommended_ingredients (85)"]
        end
        subgraph LE["efficacy 다리 — 성분 사전"]
            LEQ["질의: 고민 사전에서 파생<br/>'홍조 진정'"] -->|"query 접두어"| LEE["질의 임베딩"]
            LEE -->|"코사인 유사도"| LES[("rec_efficacy<br/>효능 임베딩")]
            LES --> LET["효능 매칭 성분 top-K (2270)"]
        end
    end

    RC1 --> ONE
    LCT --> AGG
    LET --> AGG

    AGG["④ s4_candidates<br/>두 다리 병합·중복제거<br/>ID 조인 상세채움·BSTI 가점"] --> SAF["⑤ s5_safety<br/>금지제거·경고"]
    SAF --> GEN

    subgraph GEN["⑥ s6_generation — LLM 프롬프트는 여기"]
        PB["prompts.py: 프롬프트 조립<br/>= 컨텍스트 + 후보성분 + 근거청크(case·efficacy)"]
        PB --> GM["Gemini 호출 1회"]
    end

    GEN --> RESP["⑦ s7_response<br/>배지·출처 부착"] --> OUT["추천 응답"]

    LCS -.->|"미리 계산된 벡터"| OV1
    LES -.->|"미리 계산된 벡터"| OV2

    classDef embed fill:#e8f0ff,stroke:#3366cc,color:#000
    classDef llm fill:#eafbe8,stroke:#33aa33,color:#000
    classDef store fill:#f2f2f2,stroke:#888,color:#000
    classDef dict fill:#fff2e0,stroke:#cc7a00,color:#000
    class LCE,LEE,OE1,OE2 embed
    class PB,GM llm
    class OV1,OV2,LCS,LES store
    class DICT dict
```

## 0. 한눈에 보기

- **이 기능이 하는 일**: 사용자 정보(나이·성별·피부 고민)·BSTI 피부타입·보유 제품
  성분을 받아 "당신에게는 이런 성분이 좋아요(특히 지금 루틴에 없는 것)"를
  이유·주의사항·근거와 함께 돌려준다.
- **왜 RAG인가**: LLM에게 곧바로 추천을 시키면 그럴듯한 거짓(환각)을 만든다. 그래서
  먼저 신뢰할 수 있는 자료를 **검색(Retrieval)** 하고, 그 자료만 근거로 LLM이 답을
  **생성(Generation)** 하게 한다 — 이것이 RAG(검색 증강 생성)다.
- **재료 두 가지 (이중 인덱스)**: 상담 사례 8,000건(`rec_cases` — "나와 비슷한 사람은
  어떤 답을 받았나")과 성분 지식 2,270종(`rec_efficacy` — "이 고민에 효과 있는 성분은
  뭔가"). 역할이 달라 인덱스를 둘로 나눴다.
- **구조**: 고정 시퀀스 7단계. LLM 호출은 마지막 딱 1회뿐이라 비용·지연이 예측 가능하다.
- **핵심 원칙**: 근거 없으면 생성하지 않는다(환각·비용 차단) · 위험 성분은 거른다 ·
  정확해야 하는 값(성분 ID·경고·출처)은 LLM이 아니라 코드가 붙인다.

## 1. 확정 결정 요약

| 항목 | 결정 |
|---|---|
| 파이프라인 구조 | 고정 시퀀스 7단계 (LLM 생성은 마지막 1회) — function calling 에이전트·LangGraph 미도입 |
| 검색 인덱스 | 이중 인덱스: 상담 케이스(`rec_cases`) + 성분지식(`rec_efficacy`) |
| 입력 소스 | 프로필·BSTI 결과·**화장대(user_shelf) 보유 성분**을 서버가 DB에서 직접 조회. 요청 바디 없음 (박금별 프론트 명세 정렬) |
| 온보딩 | 필수 — 프로필에 나이·피부 고민 없으면 `409 PROFILE_ONBOARDING_REQUIRED` |
| BSTI 활용 | 축 서술은 추천 모듈 소유(`bsti_traits.py`), 타입별 권장 성분은 코드 상수 `bsti_ingredients.BSTI_RECOMMENDED`(앱 `kBstiSkinTypes` 복제본)를 조회. 기피 경고는 미도입(2026-07-22) |
| 피부 고민 | 8종 enum 고정 (AI Hub 라벨과 1:1), 요청당 최대 3개 사용 |
| 출력 스코프 | 성분 추천 + **추천 성분 함유 제품 추천**(⑧, 2026-07-22 추가). 제품은 코드 조인·커버리지 순, LLM 미관여 |
| 화장대(user_shelf) | **v1 포함으로 변경 (2026-07-10)** — 보유 중 표시 + 결핍 보완(보유 성분 순위 하향·새 성분 우선). 성분 간 궁합·충돌 분석은 근거 데이터가 없어 v1.1+ (§7). 화장대가 비어 있으면 보정 없이 진행 |
| 안전성 정책 | 식약처 `금지`=후보 제외, `한도`=경고 + 미매핑 성분 명칭 보조 검사 + 알레르기 25종·BSTI 기피 경고 + **임신·수유 금기 성분 검사**. `restrictions` 적재 완료·임신 금기 목록 반영이 출시 조건 |
| 표시·광고 규제 | 생성 `reason`의 의약품적 효능(치료·완치 등) 표현 금지 — 프롬프트 제약 + 사후 금칙어 검사 (화장품법 §13, §2-⑥) |
| 임베딩·인덱싱 | **서지우 담당** — 요청 명세는 02 문서 §4 |

## 2. 파이프라인 흐름

```mermaid
flowchart TD
    A["① 컨텍스트 조립<br/>user_profiles(bsti_type) + user_shelf 조회"]
    B["② 질의 구성<br/>고민별 템플릿 (최대 3개)"]
    C["③ 검색 s3_retrieval<br/>rec_cases top 3 + rec_efficacy top 5<br/>현재 키워드 · 목표 벡터(§0-4)"]
    D["④ 후보 성분 집계<br/>중복 제거 · ingredient_id 조인 · BSTI 가점 · 보유 성분 하향"]
    E["⑤ 안전성 필터<br/>금지 제외 · 경고 3종 부착"]
    F["⑥ 생성 — LLM 호출 1회<br/>Gemini Flash · 근거 주입 · 구조화 JSON"]
    P["⑧ 제품 추천<br/>추천 성분 함유 제품 역조회 · 커버리지 순 · LLM 미관여"]
    G["⑦ 응답 조립<br/>고시 배지 · 제품 · 프론트 계약 매핑"]

    ONB(["409 PROFILE_ONBOARDING_REQUIRED"])
    INS(["200 insufficient_evidence<br/>(생성 호출 없음)"])

    A -- "나이·고민 없음" --> ONB
    A --> B --> C
    C -- "전부 임계값 미달" --> INS
    C --> D --> E
    E -- "필터 후 후보 0개" --> INS
    E --> F --> P --> G
```

모든 단계는 Langfuse 트레이스(`module:recommendations` 태그)를 남긴다.

### ① 컨텍스트 조립 — "이 사람이 누구인지 파악한다"

로그인한 사용자의 프로필과 최근 BSTI 결과를 DB에서 읽어 추천의 입력을 만든다.

- `user_profiles`에서 `age`·`gender`·`skin_concerns[]`·`bsti_type`·임신/수유 플래그를 조회한다
  (BSTI 타입 코드는 별도 테이블이 아니라 프로필의 `bsti_type` 컬럼에 있다).
- 나이 또는 피부 고민이 없으면(온보딩 미완료) `409 PROFILE_ONBOARDING_REQUIRED`.
  BSTI 결과가 없으면 BSTI 요소만 생략하고 진행한다.
- **BSTI 축 해석**: 타입 코드 4글자가 곧 4축이다 — 유·수분(O 지성/D 건성),
  민감도(S 민감/R 저항), 색소(P/N), 노화(W 주름/T 탱탱). 즉 `OSPW`를 "지성·민감·색소·주름
  경향 피부"라는 검색용 설명 문장으로 푼다. `bsti_traits.py`는 16타입 dict가 아니라
  **축별 특성 서술 사전 8개 엔트리**로 구성하고, 코드를 분해해 조합한다 (박금별 "BSTI
  근거 및 성분정보" 페이지의 축 정의를 따름). 타입별 추천 성분 목록은 여기 두지 않고
  코드 상수 `BSTI_RECOMMENDED`(앱 `kBstiSkinTypes` 복제본)에서 조회한다 (아래 `bsti_recommended`
  항목) — 앱 정의를 단일 소스로 소비해 이중 관리를 피한다.
- **보유 성분 집합**: `user_shelf`에서 사용자의 화장대 항목을 읽어 보유 성분 집합을
  만든다 — 제품 등록(`item_type=product`)은 `product_ingredients` 조인으로 성분을
  풀고, 성분 직접 등록(`item_type=ingredient`)은 그대로 담는다. **화장대가 비어
  있거나 테이블 미구현이면 빈 집합으로 진행** (BSTI 없음과 동일한 우아한 축소).
  쿼리는 `.eq("user_id", user_id)` 필수.
- **임신·수유 여부**: `user_profiles`에 임신·수유 플래그가 있으면 읽어 ⑤ 안전성
  필터의 금기 검사에 넘긴다. 플래그가 아직 없으면(온보딩 미수집) `unknown`으로
  진행 — 금기 성분을 제거하지 않고 경고만 부착한다. 단 정식 공개 전 온보딩
  수집이 필요하다 (§6·§7).
- 산출: `UserContext { age, gender, bsti_type?, bsti_recommended[], owned_ingredients[], is_pregnant?, is_nursing?, concerns[] (≤3) }`
- `bsti_recommended` 는 `bsti_ingredients.BSTI_RECOMMENDED`(앱 `kBstiSkinTypes` 복제본)에서
  타입 코드로 조회한다. 기피 성분 경고는 두지 않는다 — avoid 목록이 향료·에센셜오일 같은
  범주 위주라 추천 후보로 올라오지 않아 발화하지 않는다 (2026-07-22 결정).
- 고민이 3개를 초과하면 앞 3개만 사용(`MAX_CONCERNS = 3`) — 검색 호출 상한을
  3고민 × 2컬렉션 = 6회로 고정해 비용을 보호한다.

### ② 질의 구성 — "무엇을 검색할지 검색어를 만든다"

고민별 검색 질의를 템플릿으로 생성한다. age·gender·축 특성 서술을 조합하고 없는
요소는 생략한다. 예: *"30대 여성, 지성·민감 경향 피부의 모공 관리에 도움되는 성분"*.

### ③ Retrieval — "믿을 수 있는 자료를 찾아온다"

만든 질의로 두 인덱스를 각각 검색한다. **현재는 키워드 매칭**(효능 텍스트 ILIKE·고민 라벨
배열 겹침)이고, 의미 유사도 기반 벡터 검색은 §0-4 목표 설계다 — 전환하면 "모공 넓어짐"과
"모공이 커 보임"을 같은 뜻으로 매칭하게 된다.

| 컬렉션 | top_k | 필터 | 목적 |
|---|---|---|---|
| `rec_cases` | 3 | 고민 라벨 (`skin_concerns` 배열 겹침 매칭) | 나이·피부타입이 유사한 상담 사례 |
| `rec_efficacy` | 5 | 고민 키워드 (`efficacy` ILIKE) | 성분-효능 근거 |

- 계약: score(자료가 질의에 얼마나 맞는지 0~1 점수) 정규화, 빈 결과는 빈 리스트
  (`app/modules/recommendations/pipeline/s3_retrieval.py`). **추천 전용이다** — 성분 해설(이호영)과
  공유하려던 계획은 이호영이 자체 조회를 쓰기로 하면서 폐기했다 (2026-07-20).
- score < `MIN_RETRIEVAL_SCORE`(모듈 상수, **초기값 0.5**) 컷은 **호출자인 이 모듈이**
  수행한다. 초기값은 가설이며 Langfuse 트레이스의 실제 score 분포로 튜닝한다.
- **성능**: 고민별 질의 텍스트는 두 컬렉션에 동일하므로 질의 임베딩은 최대 3회면
  된다(6회 아님 — 임베딩 전달 옵션 또는 유틸 내부 캐시를 서지우와 협의, 02 §4).
  검색 6회는 `asyncio.gather`로 병렬 실행한다 — 순차 실행 시 검색 1~2.5초 +
  생성 4~10초로 총 5~12초가 예상되므로, 병렬화로 지연을 생성 1회 수준에 수렴시킨다.
- **희소 고민 보완** (케이스 분포 불균형 — 민감성 6건·처짐 47건, 02 문서 §1):
  배열 겹침 매칭 → 부족 시 무필터 검색 1회 fallback → 그래도 빈약하면 효능
  인덱스 근거에 자연 의존 (이중 인덱스의 존재 이유).
- **fallback 행의 score는 임계값과 같은 값(`MIN_RETRIEVAL_SCORE`)으로 고정한다**
  (2026-07-20 결정). 무필터로 가져온 행은 고민과의 관련성이 검증되지 않았으므로,
  겹침이 잡힌 행과 같은 점수를 주면 관련 있는 근거를 순위에서 밀어내고 프롬프트
  앞자리를 차지한다. 임계값과 같게 두면 근거로 살아남되 항상 최하위에 서고, ⑥의
  분량 상한에서 가장 먼저 버려진다.
  - 대안이었던 "fallback 없이 `insufficient_evidence` 반환"은 채택하지 않았다.
    희소 고민을 고른 사용자가 **항상** 빈 화면을 받게 되고, 데이터가 채워지면
    자연히 해소될 문제이기 때문이다. 벡터 검색 도입 시 재검토한다.

### ④ 후보 성분 집계 — "찾아온 자료에서 성분 목록을 뽑는다"

- **병합 규칙**: 고민별 검색 결과를 score 내림차순으로 정렬한 뒤 union하고,
  성분명 기준으로 중복을 제거한다(중복 시 최고 score 유지, 대응 `concerns`는 합집합).
- **BSTI 권장 가점**: 후보가 `context.bsti_recommended`(코드 상수 `BSTI_RECOMMENDED`, ①에서
  조립)에 있으면 score에 `BSTI_BOOST`(초기값 0.1)를 가산한다 — BSTI 타입 권장 성분이 추천
  순위에 자연 반영된다. 가산 방식·수치는 Langfuse로 튜닝한다.
- **보유 성분 하향 (결핍 보완)**: 후보가 `owned_ingredients`에 있으면 score에서
  `OWNED_PENALTY`(초기값 0.1)를 감산한다 — **제외가 아니라 하향**이다. 이미 쓰는
  성분을 빼버리면 "잘 쓰고 계세요"라는 긍정 신호를 못 주므로, 새 성분을 우선하되
  보유 성분도 상위권이면 "사용 중" 표시와 함께 남는다. 가점과 마찬가지로 dedupe
  이후 1회만 적용.
- **가점·하향은 `ingredient_id` 조인 이후에 적용한다** (2026-07-20). 조인 단계가 INCI
  영문명 후보의 표시명을 한글로 바꾸므로(상담 사례 성분의 40%), 조인 전에 적용하면
  그 후보들이 이름 불일치로 가점·하향을 통째로 못 받는다. ⑦의 `owned` 표시는 개명
  후 이름으로 판정하기 때문에 "보유 표시는 되는데 하향은 안 된" 후보가 생긴다.
- 가점 반영 후 상위 `MAX_CANDIDATES`(초기값 12)개만 생성 단계로 넘긴다 —
  케이스 `recommended_ingredients[]` 합류 시 후보가 30개 이상이 될 수 있어
  프롬프트 크기를 결정적으로 만들기 위한 상한이다.
- `ingredient_id`로 식약처 `ingredients`와 조인한다. 미매핑(NULL) 성분도 후보에
  유지하되 상세 연결 없이 반환하며, **⑤에서 명칭 기반 보조 안전성 검사를 반드시 거친다.**

### ⑤ 안전성 필터 — "위험하거나 주의가 필요한 성분을 거른다"

| 검사 | 소스 | 동작 |
|---|---|---|
| 사용제한 | `restrictions` (서지우 적재, `ingredient_id` 조인) | `금지` = 후보 제거 + 로그 / `한도` = 유지 + `limit_cond` 경고 |
| **미매핑 보조 검사** | `restrictions.name_kor`·`notice_ingr_name` 명칭 매칭 | **`ingredient_id` NULL 후보는 조인 검사를 우회하므로**, 금지·한도 목록과 성분명 매칭을 별도 수행. 그래도 확인 불가면 `안전성확인불가` 경고를 부착해 반환 |
| 착향 알레르기 | 알레르기 유발성분 25종 상수 (식약처 고시) | 해당 시 경고 — S(민감) 축 사용자는 경고 강조 |
| 성분 주의사항 | `rec_efficacy.safety_note`·`recommended_concentration` | 존재 시 경고 문구 보조 근거 |
| **임신·수유 주의** | `PREGNANCY_AVOID`(레티노이드) · `PREGNANCY_CAUTION`(살리실릭애씨드) 2단계, 02 §5 | **AVOID**: 플래그 `true` = 후보 제거 + 로그 / `unknown` = 경고. **CAUTION**: 제거하지 않고 경고만. 2026-07-20 근거 조사로 2단계 분리 — 일괄 제외는 과차단(01 §0-3) |
| 고민-성분 상충 | `rec_efficacy.recommended_skin_types` | 후보의 권장 피부타입이 사용자의 동반 고민(예: 지성용 성분 ↔ 건조·장벽손상 고민)과 상충하면 경고 — 성분↔성분 궁합(v1.1)과 다른 성분↔고민 축이라 v1에 포함 |

`restrictions`가 아직 0행이어도 코드는 동일하게 동작한다(조인 결과 없음 = 통과).
단, **이는 개발 편의이지 출시 조건이 아니다 — `restrictions` 적재 완료(행 수 > 0)를
추천 기능 배포 전 체크리스트에 포함**하고, 0행 상태에서는 기능을 공개하지 않는다.
필터 후 후보가 0개면 확인 불가 응답으로 종료한다.

### ⑥ 생성 — "LLM이 최종 추천글을 쓴다 (딱 한 번)"

여기서만 LLM을 부른다. 아래 두 규칙이 핵심이다 — 근거 없으면 안 부르고, LLM에는
"이유 쓰기"만 맡기고 정확한 값은 코드가 붙인다.

- **모든 retrieval 결과가 임계값 미달이면 생성을 호출하지 않고**
  `insufficient_evidence` 정형 응답을 반환한다 (근거 기반 생성 + 비용 보호 규칙).
- 모델: `gemini_model_for(complex_query=False)` = **Flash** (2026-07-22 변경). 여러
  근거를 종합하는 추천 최종 합성이라 llm-rag-rules의 Pro 사용 기준에는 해당하나,
  Pro 실측 지연이 사소한 프롬프트에서도 ~15초라 최대 12,000자 근거를 얹으면 30초
  타임아웃(§2-⑥ 아래)을 넘겨 502가 난다. Render 무료 티어 실사용자 502를 피하려
  Flash로 내렸다. 생성 품질은 Langfuse groundedness 평가로 관측하고 미흡하면 재검토.
- 프롬프트: 근거 청크(효능·케이스 답변·CoT step2) + UserContext(보유 성분 목록
  포함 — "현재 루틴에 없는 성분을 우선하고, 보유 성분을 추천할 땐 이미 사용
  중임을 언급"하도록 지시). 사용자 유래 값은 데이터 블록으로 격리한다
  (prompt injection 방어).
- **출력 필드 경계 — LLM이 생성하는 것과 코드가 조립하는 것을 분리한다**:
  - LLM 생성: 성분 선택(`name_kor`)·`reason`·대응 `concerns`·인용한 근거 doc_id — Pydantic
    모델을 Gemini `response_schema`로 전달해 구조를 강제한다
  - 코드 조립: `ingredient_id`·`inci`·`efficacy`(테이블 값)·`badges`·`warnings`·`sources`
    (인용 doc_id를 실제 근거 메타데이터로 해석) — LLM이 지어낼 수 없는 필드는 LLM에 맡기지 않는다
- **표시·광고 규제 준수**: 프롬프트에 "치료·완치·질환 개선 등 의약품으로 오인될
  표현 금지, 화장품 효능 범위 내 서술"을 명시하고, 생성 후 금칙어(치료·완치·질환명
  단정 등) 사후 검사로 위반 문구를 걸러 순화·재생성한다 (화장품법 §13 부당한
  표시·광고). disclaimer 한 줄로는 개별 문장의 위반을 막지 못하므로 출력 단에서
  검사한다. 성분 해설(이호영)과 공유하는 규칙.
- LLM이 선택한 성분은 ④의 후보 목록에 존재하는지 검증하고, 후보 밖 성분은 버린다(환각 차단).
- 추천 개수 `MAX_RECOMMENDED = 5` (3~5개 지시 — 모바일 화면과 생성 품질의 균형).
- JSON 파싱·스키마 검증 실패 시 1회 재시도, 재실패 시 502 + Langfuse 에러 트레이스.
- **호출 타임아웃 `GENERATION_TIMEOUT_SECONDS = 30초`** (2026-07-20 확정, §7 미결 해소).
  Flash 전환(2026-07-22) 후 실측 전체 소요는 ~13초라 30초 안에 여유 있게 완주한다
  (Pro였을 땐 이 상한을 넘겨 502가 났다 — 모델을 내린 직접 사유). 상한을 두는 이유는
  Gemini 무응답 시 워커가 무기한 묶여 인스턴스 전체가 응답하지 못하는 것을 막기
  위함이다(Render 무료 티어는 워커 수가 적다). 재시도 1회를 포함하므로 요청 전체의
  상한은 60초다 — **프론트 로딩 타임아웃은 이보다 길어야 한다.** 실제 p99는 Langfuse
  트레이스로 재조정한다.
- **근거 블록 구성**: 살아남은 후보와 연결된 청크만 넣고, 같은 `doc_id`는 한 번만
  싣고, score 내림차순으로 세운 뒤 `MAX_EVIDENCE_CHARS`(12,000자)에서 뒤쪽부터
  자른다. 정렬이 없으면 절단이 "마지막 고민의 근거를 통째로 버리는" 동작이 되어
  그 고민이 추천에서 조용히 빠진다. 중복 제거가 필요한 이유는 `진정`(홍조·민감)·
  `탄력`(주름·처짐)처럼 검색어가 겹치는 고민이 같은 행을 두 번 회수하기 때문이다.
- **금칙어 재생성이 실패해도 1차 결과를 버리지 않는다**: 재생성 응답이 통신 오류거나
  전부 후보 밖(환각)이면 1차 결과를 순화해 사용한다. 1차는 금칙어 문장만 덜어내면
  쓸 수 있는 답인데, 버리면 사용자는 근거가 있는데도 확인 불가 응답을 받는다.

### ⑦ 응답 조립 — "사용자에게 보낼 형태로 다듬는다"

기능성 고시원료 상수(미백·주름개선·자외선차단)와 대조해 배지를 부착하고,
보유 성분이면 `owned: true` + 해당 보유 제품명(`owned_products`)을 붙여 §3 계약대로
반환한다. ⑧에서 조회한 제품 목록(`products`)도 여기서 응답에 싣는다.

### ⑧ 제품 추천 — "추천 성분을 담은 실제 제품을 찾는다"

⑥이 최종 추천한 성분(`recommended_names`) 중 식약처 `ingredient_id`가 매핑된 것만 골라,
`product_ingredients`를 `ingredient_id`로 **역조회**해 그 성분을 담은 제품을 찾는다
(`s8_products.py`). **LLM 미관여** — 성분↔제품은 DB 사실 조인이라 코드가 결정적으로 붙인다.

- **커버리지 순 정렬**: 추천 성분을 여러 개 담은 제품을 위로, 동수면 배합 상위(작은
  `order_no`)를 위로. 상위 `MAX_RECOMMENDED_PRODUCTS`(초기값 5)개.
- **보유 제품 제외**: 화장대(`user_shelf`)에 이미 있는 `product_id`는 뺀다 (①에서
  `owned_product_ids`로 넘긴다).
- **우아한 축소**: 매핑된 추천 성분이 없거나(미매핑 40%) 매칭 제품이 0건이면 빈 목록.
  조회 실패도 예외가 아니라 빈 목록 — 제품은 부가 정보라 성분 추천을 깨뜨리지 않는다.

## 3. API 계약

> ⚠️ **아래 §3 응답 스키마는 2026-07-22 evidence 스타일 재편(`answer`·`cases[]`·
> `ingredients[]`·`products[]`·`advisory`) 이전 형태다.** 실제 응답은 코드
> (`schemas.py::RecommendationResponse`)가 기준이다. §3 전면 갱신은 별도 작업.
> 제품 추천은 `products: list[ProductRecommendation]`
> (`product_id`·`product_name`·`brand`·`product_url`·`main_category`·`matched_ingredients`)로
> 나간다.

`POST /api/v1/recommendations` — `Authorization: Bearer <Supabase JWT>` 필수, **요청 바디 없음**
(서버가 user_id로 프로필·BSTI를 조회한다 — 박금별 프론트 명세 정렬).

### 응답 (200)

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | string | `ok` \| `insufficient_evidence` (확인 불가 — 에러 아닌 정형 응답, 이때 아래 배열은 빈 값) |
| `message` | string \| null | 확인 불가 시 안내 문구 (정상 시 null) |
| `suggested_action` | string \| null | 확인 불가 시 행동 유도: `retry_with_other_concerns` \| `take_bsti` \| `retry_later` — 고민별 안내 문구는 박금별과 확정 (§7) |
| `recommended_ingredients` | array | 추천 성분 목록 (아래 표) |
| `recommended_products` | array | **v1은 항상 빈 배열** — MVP 후 제품 확장 시 계약 변경 없이 채운다 |
| `context_used` | object | 추천에 사용된 컨텍스트 (`age`·`gender`·`bsti_type`·`concerns`) — 프론트 표시·디버깅용 |
| `disclaimer` | string | "의학적 진단이 아닌 참고 정보" 고지 |

`recommended_ingredients[]` 항목:

| 필드 | 타입 | 설명 |
|---|---|---|
| `ingredient_id` | int \| null | 식약처 성분 연결 (미매핑이면 null — 상세 링크 없음) |
| `name_kor` / `inci` | string | 성분명 (박금별 명세 필드명 준수) |
| `concerns` | string[] | 대응하는 고민 코드 |
| `reason` | string | 생성된 개인화 추천 이유 (근거 인용 포함) |
| `efficacy` | string | 효능 요약 |
| `badges` | string[] | 기능성 고시 배지 (예: `기능성고시_미백`) |
| `owned` | boolean | 보유 성분 여부 — 화장대 제품·성분에 이미 포함돼 있으면 true |
| `owned_products` | string[] | 해당 성분을 담고 있는 보유 제품명 (성분 직접 등록이면 빈 배열, `owned=false`면 빈 배열) |
| `warnings` | array | `{type, text}` — type: `한도` \| `알레르기유발` \| `주의사항` \| `안전성확인불가` \| `임신수유주의` \| `고민상충` |
| `sources` | array | `{doc_id, title, locator}` — locator 예: `PMID:29061803` |

정상 응답 예시 (200):

```json
{
  "status": "ok",
  "message": null,
  "suggested_action": null,
  "recommended_ingredients": [
    {
      "ingredient_id": 1234,
      "name_kor": "나이아신아마이드",
      "inci": "NIACINAMIDE",
      "concerns": ["pores", "brightening"],
      "reason": "지성·민감 경향 피부의 모공·피지 관리에 도움이 되며, 현재 사용 중인 제품에는 없는 성분입니다.",
      "efficacy": "피지 조절, 멜라닌 생성 억제, 장벽 강화",
      "badges": ["기능성고시_미백"],
      "owned": false,
      "owned_products": [],
      "warnings": [],
      "sources": [
        {"doc_id": "eff_00123", "title": "나이아신아마이드 효능", "locator": "PMID:29061803"}
      ]
    },
    {
      "ingredient_id": 5678,
      "name_kor": "판테놀",
      "inci": "PANTHENOL",
      "concerns": ["sensitivity"],
      "reason": "진정·보습에 도움이 되는 성분으로, 보유하신 'OO 토너'에 이미 포함되어 있습니다.",
      "efficacy": "피부 진정, 수분 보유력 강화",
      "badges": [],
      "owned": true,
      "owned_products": ["OO 토너"],
      "warnings": [
        {"type": "알레르기유발", "text": "민감성 피부는 사용 전 첩포 검사를 권장합니다."}
      ],
      "sources": [
        {"doc_id": "case_04512", "title": "30대 민감성 상담 사례", "locator": "PMID:31234567"}
      ]
    }
  ],
  "recommended_products": [],
  "context_used": {
    "age": 32,
    "gender": "female",
    "bsti_type": "OSPW",
    "concerns": ["pores", "sensitivity", "brightening"]
  },
  "disclaimer": "본 추천은 의학적 진단이 아닌 참고 정보입니다."
}
```

확인 불가 응답 예시 (200) — 근거 부족 시 생성 호출 없이 반환:

```json
{
  "status": "insufficient_evidence",
  "message": "입력하신 고민에 대해 신뢰할 만한 추천 근거를 찾지 못했습니다.",
  "suggested_action": "take_bsti",
  "recommended_ingredients": [],
  "recommended_products": [],
  "context_used": {
    "age": 32,
    "gender": "female",
    "bsti_type": null,
    "concerns": ["sensitivity"]
  },
  "disclaimer": "본 추천은 의학적 진단이 아닌 참고 정보입니다."
}
```

### 에러 — 공통 포맷 `{"error": {"code", "message"}}`

| 상태 | code | 상황 |
|---|---|---|
| 401 | `AUTH_MISSING_TOKEN` / `AUTH_INVALID_TOKEN` | JWT 없음·무효 (conventions 예약 코드, 전역 핸들러 보장) |
| 409 | `PROFILE_ONBOARDING_REQUIRED` | 프로필에 나이 또는 피부 고민 없음 → 온보딩 화면 유도 |
| 502 | `LLM_UPSTREAM_ERROR` | Gemini 호출 실패 (1회 재시도 후) |
| 503 | `DB_UNAVAILABLE` | Supabase 연결 실패 |

에러 응답 예시 (409 — 온보딩 미완료):

```json
{
  "error": {
    "code": "PROFILE_ONBOARDING_REQUIRED",
    "message": "추천을 받으려면 먼저 나이와 피부 고민을 입력해 주세요."
  }
}
```

모듈 코드는 conventions의 `<도메인>_<사유>` 형식을 따른다. 409·502·503은
conventions 상태 코드 목록에 함께 추가한다 (공통 인프라 소유 = 김민경).

## 4. 에러 처리·엣지 케이스

- retrieval 전부 임계값 미달 → 생성 호출 없이 확인 불가 응답 (불필요 과금 차단)
- 안전성 필터 후 후보 0개 → 확인 불가 응답 + 제거 사유 로그
- BSTI 결과 없음 → BSTI 요소(축 서술·가점·기피 경고)만 생략 / 온보딩 미완 → 409
- 화장대 비어 있음·`user_shelf` 미구현 → 보유 성분 보정(하향·표시)만 생략하고 진행
- 보유 제품이 products DB(현재 올리브영 351개)에 없으면 그 제품의 성분은 집합에
  못 들어간다 — 커버리지 한계는 02 §6 기록, 성분 직접 등록이 보완 수단
- 희소 고민 → §2-③의 3단계 보완

## 5. 구현 마일스톤·테스트 전략

**중간발표(7/16) 데모 경로**: 임베딩(서지우)이 완료되기 전에도 전 단계를 구현·시연할
수 있도록, `retrieve()`가 목 스텁(고정 청크 반환)인 상태에서 ①~⑦ end-to-end를
먼저 완성한다 (기존 계약의 "임베딩 완료 전 목 스텁" 합의 그대로). 중간발표는
**목 기반 end-to-end 데모 + 실데이터 적재 리포트**로 시연하고, 서지우 인프라 완료
시 스텁만 교체한다 — 호출부 무변경.

- **단위** (`retrieve()`·Gemini 목): 축 사전 8엔트리·16타입 분해 전수, 질의 템플릿,
  후보 집계(병합·dedupe·BSTI 가점·**보유 성분 하향·`owned` 표시**·`MAX_CANDIDATES`
  상한), 안전성 필터 5종(미매핑 보조 검사 포함), 임계값 컷, LLM 선택 성분의 후보
  검증(환각 차단), 확인 불가 분기, 온보딩 409, **화장대 빈 집합 시 보정 생략**
- **명시 시나리오**: 민감성 단독 고민 사용자(케이스 6건뿐 — 확인 불가를 가장 자주
  만나는 집단)의 fallback·확인불가·`suggested_action` 경로를 별도 테스트 케이스로 둔다
- **라우터 통합**: 200 정상 / 200 확인불가 / 401 / 409 (기존 `tests/` 패턴 준수)
- **품질 평가** (설계만, 구현은 후속): AI Hub Validation 1,000건 held-out으로
  recall@k(추천 성분 ∩ 정답 케이스 성분) 측정 + Langfuse 기록.
  단, VL에 과각질/악건성 0건·민감성 1건 — 평가 커버리지 한계를 전제한다.
  recall@k는 "성분이 맞았나"만 재고 희소 고민은 정량 평가 사각지대이므로,
  **생성 `reason`이 검색 근거에 실제로 근거하는지(groundedness) LLM-as-judge
  평가**를 보완한다 — 이미 남기는 Langfuse 트레이스에 연결한다.

## 6. LLM·RAG 규칙 준수 매핑

[llm-rag-rules.md](../rules/llm-rag-rules.md) 대비 — 근거 기반 생성·확인 불가 정형
응답(§2-⑥), 출처 포함(§3 `sources`), injection 방어(§2-⑥), Langfuse 필수(§2),
비용 보호(§2-① 검색 상한, §2-⑥ 생성 스킵). 모델은 Pro 사용 기준에 해당하나 지연
때문에 Flash로 운용한다(§2-⑥ 2026-07-22) — llm-rag-rules 대비 의도된 예외.

## 7. 협의·미결 항목

기한이 지나도 합의가 안 되면 **"미결 시 기본값"으로 구현을 진행**한다 — 협의
지연이 크리티컬 패스를 막지 않게 하기 위함이다.

| 항목 | 상대 | 내용 | 기한(제안) | 미결 시 기본값 |
|---|---|---|---|---|
| BSTI 권장·기피 소비 — **해소(2026-07-22)** | 박금별 | 박금별 DB 매핑 대신 코드 상수 `BSTI_RECOMMENDED`(앱 `kBstiSkinTypes` 복제본) 채택 — 권장 성분은 `name_kor` 일치로 가점, 기피 경고는 미도입 | — | 반영 완료 |
| BSTI 기피 노출 문구 | 박금별 | 기피 성분이 추천 목록에 등장할 때 사용자 혼란 해소 문구 — BSTI 결과 화면과 동일 표현("○○ 타입은 주의") 권장 | 7/14 | BSTI 결과 화면과 동일 문구 사용 |
| 화장대 반영 | 박금별·프론트 | **v1 포함으로 변경(2026-07-10) — 박금별 명세와 정렬됨.** `user_shelf` 테이블·화장대 화면 구현 일정 공유 필요. 미구현 동안 파이프라인은 빈 화장대와 동일 동작이라 블로킹 아님 | 7/12 | 미구현이어도 추천 배포 가능 (보정만 생략) |
| 성분 궁합·충돌 분석 | 내부(김민경) | 보유 성분과의 조합 주의(레티놀+AHA 등)는 성분 간 상호작용 근거 데이터가 없어 v1 불가 — 지식 소스 확보(규칙 상수화 등) 선행 | v1.1+ | 미도입 |
| 임신·수유 온보딩 수집 | 박금별·회원 | 안전 필터의 임신·수유 금기 검사가 작동하려면 온보딩이 플래그를 수집해야 함 (§2-①·⑤) | 7/14 | 미수집 시 `unknown` 처리 → 금기 성분에 일괄 경고 부착(제거 아님). **정식 공개 전 수집 필수** |
| 응답 지연·동시성 UX | 박금별·프론트 | Flash 생성 실측 전체 ~13초(2026-07-22 Pro→Flash)·스트리밍 없음·v1 캐시 없음 — 로딩 표시·타임아웃·동시요청 정책 합의 | 7/14 | **서버 타임아웃 30초 확정(2026-07-20, §2-⑥) — 재시도 포함 요청 상한 60초. 프론트 로딩 타임아웃은 이보다 길게 잡아야 하며 박금별에게 통보 필요.** 동시요청 정책은 v1 미도입(캐시와 함께 v1.1) |
| 피부고민 표준 코드 | 박금별 | 8종 코드 상수(`app/common`) 발행 — 온보딩·추천 공용 (박금별 명세 파트 D-2 응답) | 7/11 | 김민경이 코드안 발행 후 통보 |
| BSTI 온보딩 필수 여부 | 박금별 | 현재 설계는 BSTI를 온보딩 필수에서 제외 — 기획 확인 필요 | 7/12 | 필수 제외 유지 |
| PMID 출처 소비자 표기 | 박금별·프론트 | `PMID:...`는 일반 소비자에게 소음 — "임상 연구 N건" + 링크 형태 권장 | 7/14 | 프론트가 `sources` 개수만 표기, locator는 상세 화면에서 링크 |
| 임베딩·인덱싱·RPC | 서지우 | 02 문서 §4 요청 명세 (기한·폴백 포함) | 02 §4 참조 | 02 §4의 지연 폴백 적용 |
| 응답 캐시·생성 결정성 | 내부(김민경) | 캐시 미적용 시 v1은 재요청마다 추천이 바뀌어 신뢰가 저하됨 — `(user_id, 프로필 해시, bsti_result_id)` 키 캐시는 llm-rag-rules "결정적 호출 캐시 우선" 검토 대상 | v1.1 | v1은 생성 temperature를 낮춰 변동을 완화, 키 캐시는 Langfuse 비용 관측 후 v1.1 |

## 구성 근거

- 고정 시퀀스를 택한 이유: LLM 호출 1회로 비용·지연이 예측 가능하고, LangGraph
  미도입·챗 UI 없음 확정과 정합하며, 단계별 테스트가 쉽다. function calling
  에이전트는 호출 횟수 비결정성, 케이스 재사용안은 출처 규칙 위반으로 버렸다.
- BSTI 성분 매핑을 추천 모듈이 자체로 재정의하지 않고 앱 정의의 복제본인 코드 상수
  `BSTI_RECOMMENDED`(앱 `kBstiSkinTypes`)를 단일 소스로 소비하는 이유: 같은 매핑을 여러
  곳에서 관리하면 모순(BSTI 결과는 권장인데 추천은 경고)이 필연이기 때문이다. 초기엔
  박금별 DB(`bsti_results`)를 소비하려 했으나 미머지·계약 미확정으로, 앱과 동일한 코드
  상수를 복제해 쓰는 쪽으로 확정했다(2026-07-22, §0-1).
- 화장대 보유 성분을 v1에 포함(2026-07-10 결정 변경)한 이유: cosmos의 서사가
  "제품 분석 → 추천"인데 추천이 방금 분석한 사용자 제품을 무시하면 결론이 본문과
  따로 놀고, "부족한 성분 추천"이라는 원 기능 정의 자체가 보유 성분을 전제한다.
  단 보유 성분은 **제외가 아니라 하향**(긍정 피드백 보존), 궁합·충돌은 근거 데이터
  부재로 v1.1+, 화장대 미구현·빈 상태는 우아한 축소로 처리해 일정 리스크를 차단했다.
- API 계약은 박금별 프론트 명세에 맞추되, v1 스코프 밖 요소(제품 추천)는
  명시적으로 빈 값 처리해 프론트 파손 없이 확장 여지를 남겼다.
- 안전·법규 보강(임신·수유 금기·화장품법 표시광고 가드레일)을 v1 출시 조건에
  포함한 이유: 화장품 도메인에서 임신 금기 성분 추천·의약품적 효능 표현은
  disclaimer로 방어되지 않는 실질 위해·법 위반이라, `restrictions` 적재와 같은
  급의 배포 게이트로 다뤄야 하기 때문. 단 임신 플래그 미수집 상태에선 과차단을
  피해 경고로 우아하게 축소한다.
