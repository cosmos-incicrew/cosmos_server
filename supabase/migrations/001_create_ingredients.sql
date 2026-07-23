-- pgvector 확장. 006 에서도 생성하지만(idempotent) 001 이 embedding 컬럼을 쓰므로
-- 여기서 먼저 보장한다 — 신규 환경에서 001 부터 순서대로 돌 때 vector 타입 부재로
-- 마이그레이션이 실패하던 문제를 막는다.
create extension if not exists vector with schema extensions;

create table ingredients (
  ingredient_id bigint primary key,   -- 외부(식약처) ID 를 그대로 적재 (자동생성 아님)
  name_kor text not null,             -- 코드·라이브 DB 기준 (구 name_kr 에서 정정)
  name_eng text,                      -- 구 name_en 에서 정정
  cas_no text,
  origin_definition text,
  purpose_formulation text,
  origin_ingredients text,
  embedding extensions.vector(1536)
);
