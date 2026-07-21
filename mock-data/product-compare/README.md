# 제품 성분·제한사항 Mock Supabase 데이터

`supabase-tables.json`은 단일 제품 성분 조회와 제품 비교 API를 실제 Supabase 없이 검증하기
위한 테스트 전용 데이터입니다.
키는 Supabase 테이블명이며, 각 행은 현재 데이터베이스 컬럼과 자료형을 따릅니다.

- 정상 비교용 제품 4개와 성분 연결 정보
- nullable 외래 키 처리를 검증하는 성분 매핑 대기 제품 1개
- 성분 기본명
- 아직 실제 DB에 적재되지 않은 제한사항 예시 2개

테스트는 실제 `SupabaseIngredientSearchRepository`와 `SupabaseProductCompareRepository`에
Supabase와 같은 조회 인터페이스를 제공하여 라우터 → 서비스 → 저장소 → Mock Supabase 흐름을
검증합니다. 운영 환경 설정이나 실제 DB 데이터에는 영향을 주지 않습니다.

```bash
uv run pytest tests/modules/ingredient_search tests/modules/product_compare -q
```
