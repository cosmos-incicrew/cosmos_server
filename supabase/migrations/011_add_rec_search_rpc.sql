-- ③ 벡터 검색 RPC. 코사인 유사도(1 - <=>) 상위 match_count 행.
-- order by embedding <=> query_embedding 형태를 유지해야 HNSW 인덱스를 탄다
-- (정렬식을 함수로 감싸면 인덱스를 못 탄다). 소유: 김민경. 설계 docs/design/03.

create or replace function public.match_rec_cases(
    query_embedding extensions.vector(1536),
    match_count int
)
returns table (
    case_id text,
    target_concern text,
    skin_type text,
    skin_concerns text[],
    question text,
    answer text,
    cot jsonb,
    recommended_ingredients text[],
    evidence_sources text[],
    gender text,
    age smallint,
    score double precision
)
language sql
stable
as $$
    select
        case_id, target_concern, skin_type, skin_concerns, question, answer, cot,
        recommended_ingredients, evidence_sources, gender, age,
        1 - (embedding <=> query_embedding) as score
    from public.rec_cases
    where embedding is not null
    order by embedding <=> query_embedding
    limit match_count;
$$;

create or replace function public.match_rec_efficacy(
    query_embedding extensions.vector(1536),
    match_count int
)
returns table (
    id bigint,
    inci text,
    name_kr text,
    efficacy text,
    safety_note text,
    recommended_concentration text,
    recommended_skin_types text,
    regulation_note text,
    reference_source text,
    ingredient_id bigint,
    score double precision
)
language sql
stable
as $$
    select
        id, inci, name_kr, efficacy, safety_note, recommended_concentration,
        recommended_skin_types, regulation_note, reference_source, ingredient_id,
        1 - (embedding <=> query_embedding) as score
    from public.rec_efficacy
    where embedding is not null
    order by embedding <=> query_embedding
    limit match_count;
$$;
