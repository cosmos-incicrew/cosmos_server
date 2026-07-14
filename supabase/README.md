# supabase/

DB 스키마의 진실 공급원. 모든 스키마 변경은 Supabase CLI 마이그레이션으로만 한다.

- CLI 설치 후 `supabase init`으로 이 디렉터리를 초기화할 것 (담당: 지우, 리뷰: 민경)
- 마이그레이션 생성: `supabase migration new <설명>`
- SQLAlchemy/Alembic은 쓰지 않는다 (아키텍처 설계서 8장 결정)


# DB 작업 정리

## 폴더 구조
```
db_work/
├── migrations/
│   ├── 001_create_ingredients.sql   -- ingredients 테이블 생성
│   └── 002_create_synonyms.sql  -- synonyms 테이블 생성
│   └── 003_create_restrictions.sql  -- restrictions 테이블 생성
│   └── 004_create_products.sql  -- products 테이블 생성
│   └── 005_create_product_ingredients.sql  -- product_ingredients 테이블 생성
├── ad_hoc_queries/   
│   └── check_null.sql  -- product_ingredients 테이블에서 ingredient_id가 null인 row 찾기
│   └── reorder_order_no.sql  -- product_ingredients 테이블에서 order_no 압축 및 재정렬
│   └── reorder_id.sql  -- product_ingredients 테이블에서 id 압축 및 재정렬
│   └── change_product_num.sql  -- product_ingredients 테이블에서 product_num 일괄 변경
│   └── chunk_ingredients.sql  -- 특정 문자를 기준으로 성분 chunking 후 product_ingredients에 적재
│   └── add_order_no.sql  -- product_ingredients 테이블에서 order_no 일괄 증가 및 감소
│   └── add_ingredient.sql  -- ingredients 테이블에 row 추가
│   └── copy_product.sql  -- products 테이블에서 row 복사
└── scripts/
    └── load_ingredients.py      -- kcia_ingredients.json 적재 스크립트
    └── load_synonyms.py      -- standard_names_list.json 적재 스크립트
    └── load_products.py      -- *_c.csv 적재 스크립트 - products, product_ingredients 테이블
```

## 적용 순서

1. **마이그레이션 실행** (Supabase SQL Editor에서 순서대로)
   ```
   001_create_ingredients.sql
   002_create_synonyms.sql
   003_create_restrictions.sql
   004_create_products.sql
   005_create_product_ingredients.sql
   ```

2. **성분 데이터 적재**
   ```bash
   cd scripts
   pip install psycopg2-binary python-dotenv
   # .env 파일에 SUPABASE_DB_URL 설정 후
   python load_ingredients.py --file ../../kcia_ingredients.json
   ```

3. **이명 데이터 적재**
   ```bash
   cd scripts
   pip install psycopg2-binary python-dotenv
   # .env 파일에 SUPABASE_DB_URL 설정 후
   python load_synonyms.py --file ../../standard_names_list.json
   ```

4. **이명 데이터 적재**
   ```bash
   추가 예정
   ```

5. **제품 데이터 적재**
   ```bash
   cd scripts
   pip install psycopg2-binary python-dotenv
   # .env 파일에 SUPABASE_DB_URL 설정 후
   python load_products.py --file ../../*_c.csv
   ```
