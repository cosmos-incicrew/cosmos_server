create table product_ingredients (
  id integer generated always as identity primary key,
  product_id integer references products(id),
  ingredient_id bigint references ingredients(ingredient_id),  -- 001 ingredients.ingredient_id(bigint)와 타입 일치
  raw_name text,
  order_no integer
);

create index on product_ingredients (product_id);
create index on product_ingredients (ingredient_id);