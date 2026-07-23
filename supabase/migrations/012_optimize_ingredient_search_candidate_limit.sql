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
      'standard_name_kor'::text as match_source,
      null::bigint as source_match_count
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
    limit (select bounded_limit + 1 from search_input)
  ),
  english_name_matches as (
    select
      ingredient.ingredient_id,
      ingredient.name_kor,
      ingredient.name_eng,
      null::text as synonym,
      'standard_name_eng'::text as match_source,
      null::bigint as source_match_count
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
    limit (select bounded_limit + 1 from search_input)
  ),
  synonym_matches as (
    select
      ingredient.ingredient_id,
      ingredient.name_kor,
      ingredient.name_eng,
      synonym.synonym,
      'synonym'::text as match_source,
      null::bigint as source_match_count
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
    limit (select bounded_limit + 1 from search_input)
  )
  select * from korean_name_matches
  union all
  select * from english_name_matches
  union all
  select * from synonym_matches;
$$;

revoke execute on function public.search_ingredient_candidates(text, integer) from public;
revoke execute on function public.search_ingredient_candidates(text, integer) from anon;
revoke execute on function public.search_ingredient_candidates(text, integer) from authenticated;
grant execute on function public.search_ingredient_candidates(text, integer) to service_role;
