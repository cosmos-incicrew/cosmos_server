select *
from product_ingredients
where ingredient_id is null
order by product_id, order_no;