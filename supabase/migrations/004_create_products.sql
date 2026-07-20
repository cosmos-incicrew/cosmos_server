create table products (
  id integer primary key,
  product_name text not null,
  product_num text,
  brand text,
  main_category text,
  sub_category text,
  detailed_category text,
  product_url text,
  source text,
  flagship_id integer references products(id)
);