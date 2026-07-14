with ranked as (
  select
    id,
    row_number() over (
      partition by product_id
      order by order_no nulls last, id
    ) as new_order_no
  from product_ingredients
)
update product_ingredients pi
set order_no = r.new_order_no
from ranked r
where pi.id = r.id;