# cosmos_server

cosmos 백엔드 API 서버 — 화장품 전성분 해설 · 다중 제품 교차 조회 · BSTI 기반 성분 추천.

저장소를 처음 클론했다면 이 문서 하나로 셋업부터 자기 모듈 첫 구현까지 간다.
지켜야 할 규칙은 [docs/rules/conventions.md](docs/rules/conventions.md)에 있다.

## 요구 사항

Python 3.12+, [uv](https://docs.astral.sh/uv/), (DB를 건드리면) [Supabase CLI](https://supabase.com/docs/guides/cli).

## 셋업

```bash
uv sync                 # 의존성 설치, .venv 자동 생성
cp .env.example .env    # 환경 변수 채우기
```

`.env`의 빈 값을 채운다. **실제 값은 저장소에 없다** — 팀 공유 시크릿이라 커밋 금지다.

| 변수 | 어디서 |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | 공유 Supabase 프로젝트 → Settings → API (접근 요청: 김민경) |
| `KAKAO_ADMIN_KEY` | Kakao Developers → 앱 설정 → 앱 키 → Admin 키. 회원 탈퇴 시 카카오 앱 연결 해제에만 쓴다. 비워도 기동한다 |
| `GEMINI_API_KEY` | 팀 공유 키 (노션 / 김민경) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse 프로젝트 Settings (접근: 김민경) |
| `GEMINI_MODEL_*`, `LANGFUSE_HOST`, `LOG_LEVEL` | 기본값이 `app/core/config.py`에 있음. 바꿀 때만 지정 |

필수값이 하나라도 비면 서버가 **기동 시점에** 즉시 실패한다.

> `SUPABASE_JWT_SECRET`은 더 이상 쓰지 않는다 (2026-07-21). Supabase가 액세스 토큰을
> ES256으로 서명하도록 바뀌어, 서버는 JWKS 공개키로 검증한다 (`app/core/auth.py`).
> 기존 `.env`에 줄이 남아 있어도 기동에는 문제없다 — 지워도 된다.

실행·확인:

```bash
uv run uvicorn app.main:app --reload
curl http://localhost:8000/health         # {"status":"ok"} — 인증 없이 200
curl http://localhost:8000/health/ready   # Supabase 연결까지 확인, 실패 시 503
open http://localhost:8000/docs           # 자동 생성 API 문서(Swagger)
```

`/health/ready`가 503이면 `SUPABASE_URL`이 틀렸거나 프로젝트 접근이 안 되는 것이다.

## 모듈 구현 (501 스텁 → 실제)

지금 모든 엔드포인트는 501(NOT_IMPLEMENTED) 스텁이다. 자기 모듈을 아래 순서로 살린다.
담당은 [아래 모듈 표](#모듈)를 본다.

```bash
git switch -c feat/ingredient-search-tsvector   # main 직접 커밋 금지
```

모듈은 3단계 구조이고 의존 방향은 `router.py → service.py` 한 방향이다.
**모듈 간 직접 import는 금지**(`import-linter`로 강제). 안쪽부터 채우면 손이 덜 간다.

**① `schemas.py` — 입출력 모델 (`~Request`/`~Response`)**

```python
from pydantic import BaseModel


class IngredientItem(BaseModel):
    ingredient_id: int
    name_ko: str


class SearchIngredientsResponse(BaseModel):
    items: list[IngredientItem]
```

**② `service.py` — 로직. 외부 서비스는 `app/core`를 통해서만**

```python
from app.core.supabase import get_supabase
from app.modules.ingredient_search.schemas import IngredientItem


async def search_ingredients(query: str, limit: int) -> list[IngredientItem]:
    client = await get_supabase()  # get_supabase()는 비동기 — await 필수
    rows = await client.rpc("search_ingredients", {"q": query, "n": limit}).execute()
    return [IngredientItem(**row) for row in rows.data]
```

**③ `router.py` — 501 스텁을 지우고 실제 엔드포인트로**

```python
from fastapi import APIRouter

from app.modules.ingredient_search import service
from app.modules.ingredient_search.schemas import SearchIngredientsResponse

router = APIRouter(prefix="/api/v1/ingredients", tags=["ingredient_search"])


@router.get("/search", response_model=SearchIngredientsResponse)
async def search_ingredients(query: str, limit: int = 20) -> SearchIngredientsResponse:
    items = await service.search_ingredients(query, limit)
    return SearchIngredientsResponse(items=items)
```

`prefix`는 [conventions.md](docs/rules/conventions.md)의 모듈별 prefix 표와 일치해야 한다.

**인증이 필요하면** `verify_jwt`를 의존성으로 주입한다. 반환값이 `user_id`다.
서버는 service_role 키로 접근해 RLS가 적용되지 않으므로, 사용자 소유 데이터 쿼리는
반드시 `user_id`로 필터링한다(`.eq("user_id", user_id)`). — [conventions.md](docs/rules/conventions.md) "데이터 접근 보안".

```python
from typing import Annotated
from fastapi import Depends
from app.core.auth import verify_jwt

@router.post("")
async def create_recommendations(
    body: RecommendRequest,
    user_id: Annotated[str, Depends(verify_jwt)],
) -> RecommendResponse: ...
```

**Gemini를 호출하는 모듈**(`ingredient_detail`·`recommendations`)은
[llm-rag-rules.md](docs/rules/llm-rag-rules.md)를 예외 없이 따른다.

## 검증 (머지 게이트)

머지 전 아래가 모두 통과해야 한다.

```bash
uv run pytest            # 테스트 (스텁을 살렸으면 tests/test_routers.py의 STUB_ENDPOINTS에서 그 경로를 뺀다)
uv run ruff check .      # 린트
uv run ruff format .     # 포맷
uv run mypy              # 타입 검사
uv run lint-imports      # 모듈 독립성 검사
```

다 통과하면 셀프 리뷰 후 PR을 연다. PR 절차는 [conventions.md](docs/rules/conventions.md).

## 구조

- `app/core/` — 설정·인증·외부 클라이언트 (담당: 김민경)
- `app/modules/<모듈>/` — 기능 모듈. `router.py`(엔드포인트) → `service.py`(로직) → `schemas.py`(모델)
- `docs/rules/` — 개발 규칙: [architecture.md](docs/rules/architecture.md) · [conventions.md](docs/rules/conventions.md) · [llm-rag-rules.md](docs/rules/llm-rag-rules.md)
- `docs/design/` — 기능별 설계서
- `supabase/` — DB 마이그레이션 (Supabase CLI)

## 모듈

| 모듈 | 기능 | 담당 |
|---|---|---|
| `ingredient_search` | 제품명 검색·제품→성분 ID 확장 / 성분 표준명·이명 검색 | 박영기 |
| `ingredient_detail` | 개별 성분 해설·주의사항 | 이호영 |
| `product_compare` | 2개 이상 제품의 구조화 성분 비교 | 박영기 |
| `bsti` | BSTI 16타입 검사 | 박금별 |
| `recommendations` | 성분 추천 Agent | 김민경 |
