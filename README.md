# cosmos_server

cosmos 백엔드 API 서버 — 화장품 전성분 해설 · 2개 제품 교차 조회 · BSTI 기반 성분 추천.

## 요구 사항

- Python 3.12+, [uv](https://docs.astral.sh/uv/)

## 시작하기

```bash
uv sync                          # 의존성 설치
cp .env.example .env             # 환경 변수 채우기 (팀 공유 값은 노션 참고)
uv run uvicorn app.main:app --reload
curl http://localhost:8000/health   # {"status":"ok"}
```

## 검증

```bash
uv run pytest                    # 테스트
uv run ruff check .              # 린트
uv run ruff format .             # 포맷
uv run mypy                      # 타입 검사
uv run lint-imports              # 모듈 독립성 검사
```

## 구조

- `app/core/` — 설정·인증·외부 클라이언트 (담당: 민경)
- `app/modules/<모듈>/` — 기능 모듈. `router.py`(엔드포인트) → `service.py`(로직) → `schemas.py`(모델)
- `docs/` — 팀 문서. 시작은 [getting-started.md](docs/getting-started.md), 규칙은 [conventions.md](docs/conventions.md)
- `supabase/` — DB 마이그레이션 (Supabase CLI)

| 모듈 | 기능 | 담당 |
|---|---|---|
| `ingredient_search` | 제품명·성분명 검색 | 영기 |
| `ingredient_detail` | 개별 성분 해설·주의사항 | 호영 |
| `product_compare` | 멀티 제품 교차 조회 | 영기 |
| `bsti` | BSTI 16타입 검사 | 금별 |
| `recommendation` | 성분 추천 Agent | 민경 |
