# 개발 시작하기

- **작성일**: 2026-07-09
- **작성자**: 김민경

- **대상**: 저장소를 처음 클론하는 팀원. 환경 셋업부터 자기 모듈 첫 구현까지 이 문서 하나로 간다
- **관련 문서**: 지켜야 할 규칙 [conventions.md](conventions.md) · LLM 호출 규칙 [llm-rag-rules.md](llm-rag-rules.md)

## 1. 셋업

필요 도구: Python 3.12+, [uv](https://docs.astral.sh/uv/), (DB를 건드리면) [Supabase CLI](https://supabase.com/docs/guides/cli).

```bash
uv sync                 # 의존성 설치, .venv 자동 생성
cp .env.example .env    # 환경 변수 채우기
```

`.env`의 빈 값을 채운다. **실제 값은 저장소에 없다** — 팀 공유 시크릿이라 커밋 금지다.

| 변수 | 어디서 |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET` | 공유 Supabase 프로젝트 → Settings → API (접근 요청: 김민경) |
| `GEMINI_API_KEY` | 팀 공유 키 (노션 / 김민경) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse 프로젝트 Settings (접근: 김민경) |
| `GEMINI_MODEL_*`, `LANGFUSE_HOST`, `LOG_LEVEL` | 기본값이 `app/core/config.py`에 있음. 바꿀 때만 지정 |

필수값이 하나라도 비면 서버가 **기동 시점에** 즉시 실패한다. 실행·확인:

```bash
uv run uvicorn app.main:app --reload
curl http://localhost:8000/health         # {"status":"ok"} — 인증 없이 200
curl http://localhost:8000/health/ready   # Supabase 연결까지 확인, 실패 시 503
open http://localhost:8000/docs           # 자동 생성 API 문서(Swagger)
```

`/health/ready`가 503이면 `SUPABASE_URL`이 틀렸거나 프로젝트 접근이 안 되는 것이다.

## 2. 모듈 구현 (501 스텁 → 실제)

지금 모든 엔드포인트는 501(NOT_IMPLEMENTED) 스텁이다. 자기 모듈을 아래 순서로 살린다.
담당은 [conventions.md](conventions.md)의 모듈 담당 표를 본다.

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

`prefix`는 [conventions.md](conventions.md)의 모듈별 prefix 표와 일치해야 한다.

**인증이 필요하면** `verify_jwt`를 의존성으로 주입한다. 반환값이 `user_id`다.
서버는 service_role 키로 접근해 RLS가 적용되지 않으므로, 사용자 소유 데이터 쿼리는
반드시 `user_id`로 필터링한다(`.eq("user_id", user_id)`). — [conventions.md](conventions.md) "데이터 접근 보안".

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

**Gemini를 호출하는 모듈**(`ingredient_detail`·`product_compare`·`recommendations`)은
[llm-rag-rules.md](llm-rag-rules.md)를 예외 없이 따른다.

## 3. 완료 전 검증 (머지 게이트)

```bash
uv run pytest            # 테스트 (스텁을 살렸으면 tests/test_routers.py의 STUB_ENDPOINTS에서 그 경로를 뺀다)
uv run ruff check .      # 린트
uv run ruff format .     # 포맷
uv run mypy              # 타입 검사
uv run lint-imports      # 모듈 독립성 검사
```

다 통과하면 셀프 리뷰 후 PR을 연다. PR 절차는 [conventions.md](conventions.md).
