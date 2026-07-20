create table ingredients (
  ingredient_id bigint primary key,
  name_kr text not null,
  name_en text,
  cas_no text,
  origin_definition text,
  purpose_formulation text,
  embedding vector(1536)
);
