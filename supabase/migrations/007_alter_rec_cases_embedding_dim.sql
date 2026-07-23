-- rec_cases.embedding 차원 복원: vector(1024) → vector(1536)
-- 배경: 006 은 1536 으로 선언했으나 실제 DB 컬럼이 1024 로 드리프트돼 있었고
--       1024 임베딩 8,000행이 채워져 있었다(이전 1024 모델 실험 흔적).
-- 결정: 임베딩 모델 비교(Notion "임베딩 모델 비교 4종") 결과 gemini-embedding-001 ·
--       1536 차원을 확정. 두 leg(rec_cases · rec_efficacy)를 1536 으로 통일한다.
-- 영향: 기존 1024 임베딩은 차원 불일치라 보존 불가 → NULL 로 폐기한다. 재적재는
--       임베딩 파이프라인(민경)에서 gemini-embedding-001 / RETRIEVAL_DOCUMENT / 1536 으로 수행.
-- HNSW 인덱스는 아직 없어(02 §4, 서지우) 컬럼 교체를 막지 않는다.
-- ALTER TYPE 은 8,000행 전체 재작성이라 statement timeout 을 넘긴다. 기존 1024
-- 임베딩은 어차피 폐기하므로 drop→add 로 교체한다(테이블 재작성 없이 즉시).

alter table public.rec_cases drop column embedding;
alter table public.rec_cases add column embedding extensions.vector(1536);

comment on column public.rec_cases.embedding is
    'vector(1536) · gemini-embedding-001. NULL = 미임베딩 (007 에서 1024→1536 복원)';
