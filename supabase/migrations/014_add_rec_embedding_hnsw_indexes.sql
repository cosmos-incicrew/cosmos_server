-- rec_cases · rec_efficacy embedding HNSW 인덱스 (코사인)
-- 설계: 02 §4 공유 벡터 인프라. 임베딩(gemini-embedding-001 / 1536)이 채워진 뒤 생성한다.
-- 검색은 코사인 유사도(1 - cosine_distance)를 쓰므로 vector_cosine_ops 로 만든다.
-- 파라미터는 pgvector 기본(m=16, ef_construction=64) — 8,000/2,270 규모엔 충분하다.

create index if not exists rec_cases_embedding_hnsw
    on public.rec_cases using hnsw (embedding extensions.vector_cosine_ops);

create index if not exists rec_efficacy_embedding_hnsw
    on public.rec_efficacy using hnsw (embedding extensions.vector_cosine_ops);
