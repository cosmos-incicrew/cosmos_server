-- match_rec_efficacy 에 product_traits 추가. 소유: 김민경. 설계 docs/design/03 §3.
--
-- constants.EFFICACY_FIELDS 는 product_traits 를 싣는데 RPC 가 반환하지 않아, ③ 검색으로
-- 온 후보는 이 값이 늘 NULL 이었다(⑦ BSTI 가지는 테이블을 직접 SELECT 해 정상). ⑩ 이
-- efficacy 앞에 잇는 선행 서술이 고민 축 성분에서만 조용히 사라진다 — `.get()` 이라
-- 예외도 안 난다.
--
-- 016 과 같은 이유로 drop 후 create 한다: `create or replace` 는 returns table 의 컬럼
-- 추가를 "반환 타입 변경"으로 보고 거부한다(cannot change return type of existing function).

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
    product_traits text,
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
        id, inci, name_kor, efficacy, product_traits, safety_note,
        recommended_concentration, recommended_skin_types, regulation_note,
        reference_source, ingredient_id,
        1 - (embedding <=> query_embedding) as score
    from public.rec_efficacy
    where embedding is not null
    order by embedding <=> query_embedding
    limit match_count;
$$;
