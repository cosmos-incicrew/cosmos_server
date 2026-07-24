# cosmos_server

화장품 전성분 해설 · 다중 제품 비교 · 개인 맞춤 성분 추천 API 서버.
FastAPI + Supabase(pgvector) + Vertex AI Gemini로 구성했습니다.

앱은 [cosmos_client](https://github.com/cosmos-incicrew/cosmos_client),
인프라는 [cosmos_infra](https://github.com/cosmos-incicrew/cosmos_infra)에 있습니다.

## 주요 기능

### 제품·성분 검색

제품 검색은 정규화한 제품명(`cleaned_product_name`)을 대상으로 표기 흔들림을 허용하는
패턴 매칭을 씁니다. 성분 검색은 후보를 DB에서 뽑은 뒤 6단계 우선순위로 정렬합니다 —
표준명 완전일치 → 이명 완전일치 → 표준명 접두 → 이명 접두 → 표준명 부분 → 이명 부분.
같은 등급 안에서는 질의와 길이 차이가 작은 쪽을 앞에 둡니다. LLM을 쓰지 않아 같은
질의에 항상 같은 순서가 나옵니다.

질의는 정규화 후 2자 이상 100자 이하만 받습니다. 성분이 매핑되지 않은 제품은 빈 결과가
아니라 "분석 불가"로 구분해 돌려주므로, 앱이 데이터가 없는 경우와 오류를 구분할 수
있습니다.

### 성분 해설

근거 조회 → 게이트 → 생성 → 출처 검증 순서로 동작합니다. `ingredients`와 `rec_efficacy`
에서 유래·효능·안전성 근거를 모으고, **해설 근거가 없으면 Gemini를 호출하지 않고**
`확인 불가`를 돌려줍니다.

생성된 문장에 근거에 없는 출처가 섞이면 검증 단계에서 걸러내고 `source_verified: false`로
표시합니다. 안전성 근거가 따로 없으면 `안전성 확인 불가`로 적습니다. 안전하다는 뜻이
아니라 판단할 자료가 없다는 뜻이고, 앱도 그렇게 표시합니다. 제품 단위 요약과 비교 해설도
같은 규칙을 따릅니다.

### 다중 제품 비교

2개 이상 최대 4개(`PRODUCT_COMPARE_MAX_COUNT`) 제품을 받아, 성분마다 어느 제품에 들어
있는지 표시합니다. 모든 제품에 있으면 `all`, 일부면 `partial`, 하나뿐이면 `single`입니다.
각 성분에는 식약처 규제 정보를 함께 붙입니다.

같은 제품을 두 번 보내거나 성분이 매핑되지 않은 제품이 섞이면 거절합니다. 이 단계에
LLM은 관여하지 않습니다. 해설이 필요하면 앱이 비교 결과를 그대로
`/ingredients/comparison-summary`로 넘깁니다.

### 맞춤 추천

프로필(나이·성별·고민), BSTI 타입, 화장대에 담긴 성분을 서버가 DB에서 읽어 성분과
제품을 추천합니다.

### 프로필

온보딩 프로필 저장·조회와 회원 탈퇴입니다. 탈퇴 시 카카오 앱 연결 해제까지 처리합니다.
서버는 service_role 키로 접근해 RLS가 적용되지 않으므로, 사용자 소유 데이터는 항상
`user_id`로 필터링합니다.

### 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/v1/products/search` | 제품명 검색 |
| `GET` | `/api/v1/products/{product_id}/ingredients` | 제품의 성분 ID 목록 |
| `POST` | `/api/v1/products/compare` | 다중 제품 성분 비교 |
| `GET` | `/api/v1/ingredients/search` | 성분 검색 (표준명·이명) |
| `GET` | `/api/v1/ingredients/{ingredient_id}/detail` | 개별 성분 해설·주의사항 |
| `POST` | `/api/v1/ingredients/names` | 성분 ID 목록 → 이름 조회 |
| `POST` | `/api/v1/ingredients/product-summary` | 제품 단위 성분 요약 |
| `POST` | `/api/v1/ingredients/comparison-summary` | 비교 결과 해설 |
| `POST` | `/api/v1/recommendations` | 개인 맞춤 성분·제품 추천 (요청 바디 없음) |
| `GET` `POST` | `/api/v1/users/me/profile` | 프로필 조회·저장 |
| `DELETE` | `/api/v1/users/me` | 회원 탈퇴 |
| `GET` | `/health` `/health/ready` | 상태 확인 (인증 불필요) |

`/api/v1/*`는 모두 Supabase access token이 필요합니다.

## 요구 사항

- Python 3.12 이상
- [uv](https://docs.astral.sh/uv/)
- [Supabase CLI](https://supabase.com/docs/guides/cli) — DB 스키마를 변경할 때만
- Google Cloud CLI — Gemini를 호출하는 기능(성분 해설·추천)을 로컬에서 쓸 때

## 설치·실행

```bash
uv sync                 # 의존성 설치, .venv 자동 생성
cp .env.example .env    # 환경 변수 파일 준비
```

`.env`에 아래 네 개를 채우면 기동합니다. 나머지는 기본값이 있습니다.

| 변수 | 어디서 |
|---|---|
| `SUPABASE_URL` `SUPABASE_SERVICE_ROLE_KEY` | 팀에서 관리하는 개발용 비밀 저장소 |
| `LANGFUSE_PUBLIC_KEY` `LANGFUSE_SECRET_KEY` | 개발 Langfuse 프로젝트 Settings |

Gemini를 호출하는 기능까지 확인하려면 `GCP_PROJECT_ID`를 채우고 로컬에서
Application Default Credentials로 로그인합니다.

```bash
gcloud auth application-default login
```

실행과 확인:

```bash
uv run uvicorn app.main:app --reload

curl http://localhost:8000/health         # {"status":"ok"} — 인증 없이 200
curl http://localhost:8000/health/ready   # Supabase 연결까지 확인, 실패 시 503
open http://localhost:8000/docs           # Swagger
```

필수값이 하나라도 비면 서버는 **기동 시점에** 즉시 실패합니다.
`/health/ready`가 503이면 `SUPABASE_URL`이 틀렸거나 프로젝트 접근이 되지 않는 경우입니다.

인증 없이 API를 시험할 때는 개발용 토큰을 발급합니다. 약 1시간 유효합니다.

```bash
uv run python scripts/dev_token.py
```

> 팀 외부에서 이 저장소를 그대로 재현하려면 자체 Supabase 프로젝트에
> `supabase/migrations/`를 적용하고, 식약처 오픈API와 제품 데이터를 직접 수집해
> `supabase/scripts/load_*.py`로 적재해야 합니다. 원본 데이터는 저장소에 포함하지
> 않습니다.

## 설정

| 변수 | 설명 |
|---|---|
| `SUPABASE_URL` `SUPABASE_SERVICE_ROLE_KEY` | Supabase 접근. 서버는 service_role로 접근하므로 RLS가 적용되지 않습니다 |
| `KAKAO_ADMIN_KEY` | 회원 탈퇴 시 카카오 앱 연결 해제에만 사용. 비워도 기동합니다 |
| `GCP_PROJECT_ID` | Vertex AI를 사용하는 GCP 프로젝트 |
| `GCP_LOCATION` | 기본값 `global`. Gemini 3.x 계열은 `global`에서만 제공됩니다 |
| `GEMINI_MODEL` | 기본값은 `app/core/config.py` 참고 |
| `LANGFUSE_PUBLIC_KEY` `LANGFUSE_SECRET_KEY` `LANGFUSE_BASE_URL` | LLM 관측 |
| `CORS_ALLOWED_ORIGINS` | 브라우저 프론트 origin의 JSON 배열. 기본값은 개발 Vercel과 로컬 웹 |
| `PRODUCT_COMPARE_MAX_COUNT` | 비교 가능한 제품 수 상한 (기본 4) |
| `LOG_LEVEL` | 기본 `INFO` |

`SUPABASE_JWT_SECRET`은 사용하지 않습니다(2026-07-21 변경). Supabase가 액세스 토큰을
ES256으로 서명하도록 바뀌어 서버는 JWKS 공개키로 검증합니다(`app/core/auth.py`).

GCE에서는 `GCP_PROJECT_ID`와 `GCP_LOCATION`만 지정하고 VM에 연결된 서비스 계정의
Application Default Credentials를 사용합니다. 서비스 계정 JSON 키는 배포하지 않습니다.

## 프로젝트 구조

```
app/
├─ core/          설정 · JWT 검증 · Supabase/Gemini/Langfuse 클라이언트
├─ common/        공통 예외·유틸
└─ modules/<모듈>/
   ├─ router.py    엔드포인트
   ├─ service.py   로직
   └─ schemas.py   요청·응답 모델
supabase/         DB 마이그레이션(Supabase CLI) · 적재 스크립트 · 스키마 문서
scripts/          개발 토큰 발급 · 데이터 적재 · 검색 품질 평가
evaluation/       검색·추천 평가 데이터셋
docs/             규칙 · 설계 · ADR · 배포 가이드
```

의존 방향은 `router.py → service.py` 한 방향이고, 모듈 간 직접 import는 금지입니다
(`import-linter`가 CI에서 강제합니다). 외부 서비스는 `app/core`를 통해서만 접근합니다.

## 개발

`main`에 직접 커밋하지 않고 브랜치에서 작업합니다. 커밋 메시지는 Conventional Commits를
따릅니다.

머지 전에 아래가 모두 통과해야 합니다.

```bash
uv run pytest            # 테스트 455개
uv run ruff check .      # 린트
uv run ruff format .     # 포맷
uv run mypy              # 타입 검사
uv run lint-imports      # 모듈 독립성 검사
```

`main`에 머지되면 GitHub Actions가 이미지를 빌드하고, `development` Environment 승인을
거쳐 API VM에 배포합니다. DB 마이그레이션은 자동 실행하지 않습니다.

## 문서

| 문서 | 내용 |
|---|---|
| [rules/architecture.md](docs/rules/architecture.md) | 레이어 구조와 의존 규칙 |
| [rules/conventions.md](docs/rules/conventions.md) | 네이밍·모듈 prefix·PR 절차·데이터 접근 보안 |
| [rules/llm-rag-rules.md](docs/rules/llm-rag-rules.md) | Gemini 호출·프롬프트·관측 규칙 |
| [design/](docs/design/) | 추천 파이프라인 · 데이터 스펙 · 벡터 검색 · 품질 개선 |
| [adr/](docs/adr/) | 호스트 분리, 인프라 저장소 분리, 트레이스 개인정보 최소화 |
| [deployment/team-guide.md](docs/deployment/team-guide.md) | 개발 서버 사용법과 배포 흐름 |
| [supabase/schema.md](supabase/schema.md) | DB 스키마 |