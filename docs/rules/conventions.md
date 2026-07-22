# 개발 규칙

- **작성일**: 2026-07-09
- **작성자**: 김민경

- **대상**: cosmos_server에 코드를 쓰는 모든 팀원. PR 전에 이 문서 기준으로 셀프 리뷰한다
- **관련 문서**: 셋업·첫 모듈 구현 [README.md](../../README.md) · LLM 호출 규칙 [llm-rag-rules.md](llm-rag-rules.md)

## 코드 스타일

- **포맷·린트:** `uv run ruff format .`·`uv run ruff check .`을 통과해야 머지할 수 있다.
  설정은 pyproject.toml 기준(line-length 100, py312). 개인 포매터 설정을 쓰지 않는다.
- **네이밍:** 함수·변수 `snake_case` / 클래스 `PascalCase` / 상수 `UPPER_SNAKE_CASE`.
- **매직 넘버·문자열 금지** — 의미 있는 이름의 상수로 추출한다
  (예: Gemini 모델명은 설정으로, 하드코딩 금지).
- **타입 힌트:** 모든 함수의 매개변수·반환값에 붙인다. `dict` 대신 Pydantic 모델을 반환한다
  (health 등 단순 상태 응답 제외).
- **Pydantic v2:** 요청은 `~Request`, 응답은 `~Response` 접미사. 모델은 각 모듈 `schemas.py`에 두고,
  공유가 필요하면 `app/common/schemas.py`로 올린다.
- **주석:** 코드가 말 못하는 "왜"만 쓴다. 무엇을 하는지 설명하는 주석은 쓰지 않는다.

## 모듈 구조

- 의존 방향은 `router.py → service.py` 한 방향. 라우터에 비즈니스 로직을 쓰지 않는다.
- **모듈 간 직접 import 금지.** 공유가 필요하면 `app/core`(외부 클라이언트·설정·인증) 또는
  `app/common`(공유 모델)으로 올린 뒤 양쪽에서 쓴다. 이 규칙은 `import-linter`가 강제한다
  (`uv run lint-imports`).
- 외부 서비스(Supabase·Gemini·Langfuse)는 반드시 `app/core`의 `get_supabase()`·`get_gemini()`·
  `get_langfuse()`로만 접근한다.
- **비동기 주의:** `get_supabase()`는 코루틴이다 — `client = await get_supabase()`로 받고 쿼리도
  `await`한다. Gemini도 `client.aio`(비동기)를 쓴다. async 라우터에서 동기 호출은 이벤트 루프를 막는다.

## 데이터 접근 보안 (RLS 우회 주의)

- 서버는 service_role 키로 접근하므로 **RLS가 적용되지 않는다.** 행 권한 검사는 코드가 직접 한다.
- 사용자 소유 데이터를 다루는 **모든** 쿼리는 `verify_jwt`가 반환한 `user_id`로 필터링한다
  (`.eq("user_id", user_id)`). 조회·수정·삭제 어느 경로든 예외 없다. 빠뜨리면 타 사용자 데이터가
  노출된다 — PR 리뷰 우선 확인 항목.

## API 설계

엔드포인트 상세(경로·필드)는 **API 명세서(Notion)가 진실 공급원**이다. 여기는 공통 규칙만 정한다.

- 모든 API는 `/api/v1` 아래, 리소스는 복수형 명사. 모듈별 prefix는 **코드와 일치해야 한다**:

  | 모듈 | prefix |
  |---|---|
  | ingredient_search | `/api/v1/products` · `/api/v1/ingredients` |
  | ingredient_detail | `/api/v1/ingredients` |
  | product_compare | `/api/v1/products` |
  | bsti | `/api/v1/bsti` |
  | recommendations | `/api/v1/recommendations` |
  | users | `/api/v1/users/me` |

- **메서드:** GET은 조회(부작용 없음), POST는 생성·실행형 작업(비교·추천·설문 제출).
- **상태 코드:** 200 성공 · 201 생성 · 400 잘못된 요청 · 401 인증 실패 · 404 없음 ·
  409 선행 조건 미충족(예: 온보딩 미완료) · 422 유효성 실패 · 500 서버 오류 ·
  501 미구현 스텁 · 502 외부 서비스(LLM 등) 호출 실패 · 503 의존 서비스(DB 등) 연결 실패.
- **에러 응답**은 전역 핸들러가 아래 포맷을 보장한다:

  ```json
  {"error": {"code": "AUTH_INVALID_TOKEN", "message": "유효하지 않은 토큰입니다."}}
  ```

  `code`는 `UPPER_SNAKE_CASE`. 예약 코드: `AUTH_MISSING_TOKEN` `AUTH_INVALID_TOKEN`
  `VALIDATION_ERROR` `NOT_IMPLEMENTED` `INTERNAL_ERROR` `HTTP_<status>`.
  모듈별 코드는 `<도메인>_<사유>` 형태로 추가한다(예: `INGREDIENT_NOT_FOUND`).
  `message`는 사용자에게 보여줄 한국어 문장으로 쓴다.
  엔드포인트에서는 `HTTPException(status_code=..., detail={"code": ..., "message": ...})`로 던진다.
- **인증:** 기본은 보호 라우터(`user_id: str = Depends(verify_jwt)`). 인증 없이 여는 건 `/health`뿐이며,
  추가하려면 팀 합의가 필요하다.

## 테스트

- 실행: `uv run pytest`. `asyncio_mode = "auto"`라 `async def test_...`를 마커 없이 쓴다.
- `tests/`는 `app/` 구조를 미러링한다. 모듈 테스트는 `tests/modules/<모듈>/test_*.py`.
- 환경 변수는 `tests/conftest.py`가 가짜 값으로 주입한다 — **실제 `.env`나 실서비스 키가 필요 없다.**
- 엔드포인트는 `TestClient(app, raise_server_exceptions=False)`로 검사한다. 이래야 500·503도
  공통 에러 포맷(`body["error"]["code"]`)으로 확인할 수 있다.
- **외부 서비스는 호출하지 않는다.** `monkeypatch`로 `get_supabase()`/`get_gemini()` 경계를 대체한다.
- 스텁을 구현했다면 `tests/test_routers.py`의 `STUB_ENDPOINTS`에서 그 경로를 빼고 정상·에러 케이스로
  대체한다. 규칙으로 정한 불변식(예: `user_id` 필터, 생성 전 retrieval 근거)은 테스트로 못 박는다.

## Git

- `main` 직접 커밋 금지. 항상 브랜치 → PR → 리뷰 → 머지. 브랜치명은 영어:
  `feat/<모듈>-<설명>`, 버그 `fix/...`, 문서 `docs/...`.
- 커밋은 Conventional Commits(영어): `feat:` `fix:` `refactor:` `docs:` `test:` `chore:`.
  하나의 커밋은 하나의 논리적 변경만.
- **`.env`·API 키·시크릿 커밋 금지.** 실수로 스테이징되면 커밋을 멈추고 팀에 알린다.
- **다른 사람 담당 모듈을 상의 없이 수정하지 않는다.**
- PR: ① diff를 처음부터 끝까지 셀프 리뷰 → ② 위 검증(pytest·ruff·mypy·lint-imports) 통과 확인 →
  ③ 아래 담당 표 기준으로 리뷰어 지정. 공통 인프라(core·common·main)는 김민경.
