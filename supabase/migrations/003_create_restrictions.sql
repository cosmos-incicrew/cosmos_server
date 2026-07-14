create table restrictions (
  restriction_id bigint generated always as identity primary key,
  ingredient_id bigint references ingredients(ingredient_id),
  name_kor text,
  notice_ingr_name text,
  regulate_type text check (regulate_type in ('금지', '한도')),
  provis_atrcl text,
  limit_cond text,
  is_registered_korea boolean,
  check (ingredient_id is not null or is_registered_korea = false)
);