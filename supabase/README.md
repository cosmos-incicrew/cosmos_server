# supabase/

DB 스키마의 진실 공급원. 모든 스키마 변경은 Supabase CLI 마이그레이션으로만 한다.

- CLI 설치 후 `supabase init`으로 이 디렉터리를 초기화할 것 (담당: 지우, 리뷰: 민경)
- 마이그레이션 생성: `supabase migration new <설명>`
- SQLAlchemy/Alembic은 쓰지 않는다 (아키텍처 설계서 8장 결정)
