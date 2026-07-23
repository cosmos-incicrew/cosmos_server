create schema if not exists extensions;
create extension if not exists pgroonga with schema extensions;

create index if not exists ingredients_search_name_kor_pgroonga_idx
on public.ingredients using pgroonga (
  (regexp_replace(lower(normalize(name_kor, NFKC)), '[^[:alnum:]]', '', 'g'))
    extensions.pgroonga_text_regexp_ops_v2
);

create index if not exists ingredients_search_name_eng_pgroonga_idx
on public.ingredients using pgroonga (
  (regexp_replace(lower(normalize(name_eng, NFKC)), '[^[:alnum:]]', '', 'g'))
    extensions.pgroonga_text_regexp_ops_v2
);

create index if not exists synonyms_search_synonym_pgroonga_idx
on public.synonyms using pgroonga (
  (regexp_replace(lower(normalize(synonym, NFKC)), '[^[:alnum:]]', '', 'g'))
    extensions.pgroonga_text_regexp_ops_v2
);

create or replace function public.search_ingredient_candidates(
  search_query text,
  result_limit integer default 100
)
returns table (
  ingredient_id bigint,
  name_kor text,
  name_eng text,
  synonym text,
  match_source text,
  source_match_count bigint
)
language sql
stable
security invoker
set search_path = ''
as $$
  with search_input as (
    select
      regexp_replace(lower(normalize(search_query, NFKC)), '[^[:alnum:]]', '', 'g')
        as normalized_query,
      greatest(1, least(result_limit, 500)) as bounded_limit
  ),
  korean_name_matches as (
    select
      ingredient.ingredient_id,
      ingredient.name_kor,
      ingredient.name_eng,
      null::text as synonym,
      'standard_name'::text as match_source,
      count(*) over () as source_match_count
    from public.ingredients as ingredient
    cross join search_input
    where search_input.normalized_query <> ''
      and regexp_replace(lower(normalize(ingredient.name_kor, NFKC)), '[^[:alnum:]]', '', 'g')
        like '%' || search_input.normalized_query || '%'
    order by
      case
        when regexp_replace(lower(normalize(ingredient.name_kor, NFKC)), '[^[:alnum:]]', '', 'g')
          = search_input.normalized_query then 0
        when regexp_replace(lower(normalize(ingredient.name_kor, NFKC)), '[^[:alnum:]]', '', 'g')
          like search_input.normalized_query || '%' then 1
        else 2
      end,
      length(
        regexp_replace(lower(normalize(ingredient.name_kor, NFKC)), '[^[:alnum:]]', '', 'g')
      ),
      ingredient.name_kor,
      ingredient.ingredient_id
    limit (select bounded_limit from search_input)
  ),
  english_name_matches as (
    select
      ingredient.ingredient_id,
      ingredient.name_kor,
      ingredient.name_eng,
      null::text as synonym,
      'standard_name'::text as match_source,
      count(*) over () as source_match_count
    from public.ingredients as ingredient
    cross join search_input
    where search_input.normalized_query <> ''
      and regexp_replace(lower(normalize(ingredient.name_eng, NFKC)), '[^[:alnum:]]', '', 'g')
        like '%' || search_input.normalized_query || '%'
    order by
      case
        when regexp_replace(lower(normalize(ingredient.name_eng, NFKC)), '[^[:alnum:]]', '', 'g')
          = search_input.normalized_query then 0
        when regexp_replace(lower(normalize(ingredient.name_eng, NFKC)), '[^[:alnum:]]', '', 'g')
          like search_input.normalized_query || '%' then 1
        else 2
      end,
      length(
        regexp_replace(lower(normalize(ingredient.name_eng, NFKC)), '[^[:alnum:]]', '', 'g')
      ),
      ingredient.name_eng,
      ingredient.ingredient_id
    limit (select bounded_limit from search_input)
  ),
  synonym_matches as (
    select
      ingredient.ingredient_id,
      ingredient.name_kor,
      ingredient.name_eng,
      synonym.synonym,
      'synonym'::text as match_source,
      count(*) over () as source_match_count
    from public.synonyms as synonym
    join public.ingredients as ingredient using (ingredient_id)
    cross join search_input
    where search_input.normalized_query <> ''
      and regexp_replace(lower(normalize(synonym.synonym, NFKC)), '[^[:alnum:]]', '', 'g')
        like '%' || search_input.normalized_query || '%'
    order by
      case
        when regexp_replace(lower(normalize(synonym.synonym, NFKC)), '[^[:alnum:]]', '', 'g')
          = search_input.normalized_query then 0
        when regexp_replace(lower(normalize(synonym.synonym, NFKC)), '[^[:alnum:]]', '', 'g')
          like search_input.normalized_query || '%' then 1
        else 2
      end,
      length(
        regexp_replace(lower(normalize(synonym.synonym, NFKC)), '[^[:alnum:]]', '', 'g')
      ),
      synonym.synonym,
      ingredient.ingredient_id
    limit (select bounded_limit from search_input)
  )
  select
    matches.ingredient_id,
    matches.name_kor,
    matches.name_eng,
    matches.synonym,
    matches.match_source,
    matches.source_match_count
  from (
    select * from korean_name_matches
    union all
    select * from english_name_matches
    union all
    select * from synonym_matches
  ) as matches;
$$;

revoke execute on function public.search_ingredient_candidates(text, integer) from public;
revoke execute on function public.search_ingredient_candidates(text, integer) from anon;
revoke execute on function public.search_ingredient_candidates(text, integer) from authenticated;
grant execute on function public.search_ingredient_candidates(text, integer) to service_role;
