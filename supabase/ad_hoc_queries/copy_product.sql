insert into products
  (product_name, cleaned_product_name, product_num, main_category,
   sub_category, detailed_category, product_url, brand, source, flagship_id)
select
  product_name, cleaned_product_name, product_num || '_copy', main_category,
  sub_category, detailed_category, product_url, brand, source, flagship_id
from products
where id = 기존id;