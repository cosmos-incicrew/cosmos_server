with raw_ingredients as (
  select '성분명' as ingredient_text
),
tokens as (
  select
    ordinality as order_no,
    trim(token) as raw_name
  from raw_ingredients,
       unnest(string_to_array(ingredient_text, ' ')) with ordinality as t(token, ordinality)
  where trim(token) <> ''
)
insert into product_ingredients (product_id, ingredient_id, raw_name, order_no)
select
  231,
  i.ingredient_id,
  t.raw_name,
  t.order_no
from tokens t
left join ingredients i on i.name_kor = t.raw_name
order by t.order_no;