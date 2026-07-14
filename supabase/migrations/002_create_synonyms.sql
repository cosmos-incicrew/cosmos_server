create table synonyms (
  synonym_id bigint generated always as identity primary key,
  ingredient_id bigint references ingredients(ingredient_id),
  name_kor text,
  synonym text,
  language text
);