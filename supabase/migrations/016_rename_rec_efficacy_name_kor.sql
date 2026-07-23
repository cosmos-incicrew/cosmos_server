-- rec_efficacy.name_kr → name_kor. `ingredients` 는 이미 name_kor(001 에서 정정)이라
-- 두 테이블의 한글명 컬럼 표기를 통일한다. 소유: 김민경. 설계 docs/design/04.
--
-- RPC 를 함께 재정의하지 않으면 match_rec_efficacy 가 사라진 컬럼을 참조해 ③ 벡터 검색이
-- 즉시 깨진다. `create or replace` 는 returns table 의 컬럼명 변경을 "반환 타입 변경"으로
-- 보고 거부하므로(cannot change return type of existing function) drop 후 create 한다.
--
-- UNIQUE(inci, name_kr)·CHECK 제약은 컬럼을 OID 로 참조하므로 rename 을 자동으로 따라간다.

alter table public.rec_efficacy rename column name_kr to name_kor;

drop function if exists public.match_rec_efficacy(extensions.vector(1536), int);

create function public.match_rec_efficacy(
    query_embedding extensions.vector(1536),
    match_count int
)
returns table (
    id bigint,
    inci text,
    name_kor text,
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
        id, inci, name_kor, efficacy, safety_note, recommended_concentration,
        recommended_skin_types, regulation_note, reference_source, ingredient_id,
        1 - (embedding <=> query_embedding) as score
    from public.rec_efficacy
    where embedding is not null
    order by embedding <=> query_embedding
    limit match_count;
$$;
