"""
clean_products.py로 정제된 CSV -> products / product_ingredients 테이블 적재

입력 CSV 컬럼(clean_products.py 출력):
  대분류, 중분류, 소분류, 브랜드, 제품명, 정제된 제품명, 제품번호, 링크, 성분,
  대표상품번호, 데이터출처

products 테이블:
  id(INTEGER, generated always as identity, PK), product_name, cleaned_product_name,
  product_num, main_category, sub_category(=중분류), detailed_category(=소분류),
  product_url, brand, source, flagship_id(INTEGER, 자기참조)

product_ingredients 테이블:
  id(PK), product_id(INTEGER FK -> products.id), ingredient_id(FK -> ingredients),
  raw_name, order_no

id는 이제 카테고리별 번호대 없이 그냥 자동증가(identity)로 채번됨.
그래서 흐름이 2단계로 나뉨:
  1) products를 먼저 insert (id는 DB가 자동으로 채번) -> product_num:id 매핑 확보
  2) 그 매핑으로 flagship_id 계산해서 update, 그리고 product_ingredients insert

성분 텍스트 처리 (매칭 순서):
  1. 콤마 기준으로 성분 청킹
  2. 1글자 토큰(예외 문자 제외)은 화합물명이 잘못 쪼개진 걸로 보고 다음 토큰과 이어붙임
  3. 매칭 시도: 원본 -> 맨 뒤 +/*./ 제거 -> 괄호 안 숫자(ppm/ppb/% 등) 있으면 그 괄호
     통째로 제거 -> 공백 제거, 순서대로 시도
  4. 그래도 매칭 안 되면 원본(raw_name) 그대로 저장
  - 대괄호/콜론 기반 라벨·구성물 표기는 전혀 건드리지 않음 (수작업으로 처리 예정)

사용법:
    python load_products.py --files "cleaned_products*.csv"
"""

import argparse
import csv
import glob
import os
import re
import sys

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 1000
UNMATCHED_CSV = "unmatched_product_ingredients.csv"


def normalize(value):
    if value is None:
        return None
    v = value.strip()
    return v if v else None


# ── 성분 텍스트 정제/매칭 관련 ──────────────────────────────

def strip_dosage_parens(text: str) -> str:
    text = re.sub(r"\([^)]*\d[^)]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_trailing_symbols(text: str) -> str:
    return text.rstrip(" +*.")


def try_match_ingredient(raw_name: str, name_to_id: dict):
    """단계별 매칭 시도.
    성공: (ingredient_id, 매칭에 성공한 정제된 텍스트) 반환
    실패: (None, None) 반환 -> 호출부에서 원본 raw_name을 그대로 써야 함
    """
    variants = []
    v0 = raw_name.strip()
    variants.append(v0)

    v1 = strip_trailing_symbols(v0)
    if v1 != v0:
        variants.append(v1)

    v2 = strip_dosage_parens(v1)
    if v2 != v1:
        variants.append(v2)

    v3 = v2.replace(" ", "")
    if v3 != v2:
        variants.append(v3)

    for variant in variants:
        if variant in name_to_id:
            return name_to_id[variant], variant

    return None, None


VALID_SINGLE_CHAR_NAMES = {"감", "귤", "금", "꿀", "락", "쌀", "콩", "팥", "황"}


def merge_isolated_short_tokens(tokens: list[str], exception_chars: set) -> list[str]:
    merged = []
    i = 0
    while i < len(tokens):
        current = tokens[i]
        last_piece = tokens[i]
        while (
            len(last_piece) == 1
            and last_piece not in exception_chars
            and i + 1 < len(tokens)
        ):
            i += 1
            last_piece = tokens[i]
            current = current + "," + last_piece
        merged.append(current)
        i += 1
    return merged


def build_multi_comma_names(conn) -> set:
    names = set()
    with conn.cursor() as cur:
        cur.execute("select name_kr from ingredients where name_kr like '%,%';")
        names.update(row[0].strip() for row in cur.fetchall() if row[0])
    with conn.cursor() as cur:
        cur.execute(
            "select synonym from synonyms where language = 'kor' and synonym like '%,%';"
        )
        names.update(row[0].strip() for row in cur.fetchall() if row[0])
    return names


def split_ingredients(raw_text: str, multi_comma_names: set, max_lookahead: int = 3) -> list[str]:
    tokens = [t.strip() for t in raw_text.split(",") if t.strip()]
    tokens = merge_isolated_short_tokens(tokens, VALID_SINGLE_CHAR_NAMES)

    merged = []
    i = 0
    while i < len(tokens):
        matched = False
        for lookahead in range(max_lookahead, 0, -1):
            if i + lookahead < len(tokens):
                candidate = ",".join(tokens[i : i + lookahead + 1])
                if candidate in multi_comma_names:
                    merged.append(candidate)
                    i += lookahead + 1
                    matched = True
                    break
        if not matched:
            merged.append(tokens[i])
            i += 1
    return merged


def build_ingredient_name_map(conn) -> dict:
    mapping = {}
    with conn.cursor() as cur:
        cur.execute(
            "select synonym, ingredient_id from synonyms where language = 'kor' and synonym is not null;"
        )
        for synonym, ingredient_id in cur.fetchall():
            key = synonym.strip()
            if key not in mapping:
                mapping[key] = ingredient_id
    with conn.cursor() as cur:
        cur.execute("select name_kr, ingredient_id from ingredients where name_kr is not null;")
        for name_kr, ingredient_id in cur.fetchall():
            mapping[name_kr.strip()] = ingredient_id
    return mapping


def process_ingredient_text(
    product_id: int, ingredient_text: str, name_to_id: dict, multi_comma_names: set,
    ingredient_rows: list, unmatched: list
):
    parts = split_ingredients(ingredient_text, multi_comma_names)

    order_no = 0
    i = 0
    while i < len(parts):
        raw_name = parts[i]  # 원본 토큰 (아직 어떤 정제도 안 됨)
        ingredient_id, matched_text = try_match_ingredient(raw_name, name_to_id)

        if (
            ingredient_id is None
            and i + 1 < len(parts)
            and raw_name
            and raw_name[-1].isdigit()
            and parts[i + 1]
            and parts[i + 1][0].isdigit()
        ):
            merged_candidate = raw_name + "," + parts[i + 1]
            merged_id, merged_matched = try_match_ingredient(merged_candidate, name_to_id)
            if merged_id is not None:
                ingredient_id = merged_id
                matched_text = merged_matched
                raw_name = merged_candidate
                i += 1

        order_no += 1
        if ingredient_id is None:
            unmatched.append((product_id, raw_name))
            stored_name = raw_name
        else:
            stored_name = matched_text

        ingredient_rows.append((product_id, ingredient_id, stored_name, order_no))
        i += 1


# ── CSV 로드 ────────────────────────────────────────────────

def find_csv_files(pattern: str) -> list[str]:
    files = []
    for part in pattern.split(","):
        part = part.strip()
        matched = glob.glob(part)
        files.extend(matched if matched else [part])
    return sorted(set(files))


def load_products_from_csv(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    cleaned = [r for r in rows if normalize(r.get("대분류")) != "대분류"]
    skipped = len(rows) - len(cleaned)
    if skipped:
        print(f"[알림] {path}에서 헤더가 섞인 행 {skipped}건을 걸러냈어요.")

    return cleaned


# ── products insert (id는 identity로 자동 채번) ─────────────

def insert_products_and_get_ids(conn, records: list[dict]) -> dict:
    """products를 insert하고, DB가 생성한 id를 product_num 기준으로 매핑해서 반환"""
    rows = []
    seen = set()

    for rec in records:
        product_num = normalize(rec.get("제품번호"))
        if not product_num or product_num in seen:
            continue
        seen.add(product_num)

        rows.append((
            normalize(rec.get("제품명")),
            normalize(rec.get("정제된 제품명")),
            product_num,
            normalize(rec.get("대분류")),
            normalize(rec.get("중분류")),
            normalize(rec.get("소분류")),
            normalize(rec.get("링크")),
            normalize(rec.get("브랜드")),
            normalize(rec.get("데이터출처")),
        ))

    insert_sql = """
        insert into products
            (product_name, cleaned_product_name, product_num, main_category,
             sub_category, detailed_category, product_url, brand, source)
        values %s
        on conflict (product_num) do nothing
        returning id, product_num
    """

    product_num_to_id = {}
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            result = execute_values(cur, insert_sql, batch, fetch=True)
            for pid, product_num in result:
                product_num_to_id[product_num] = pid
            conn.commit()
            print(f"  [products] {i + len(batch)}/{len(rows)}건 처리 완료")

    # on conflict do nothing으로 스킵된(이미 있던) 제품들의 id도 마저 채워야
    # flagship_id 계산, product_ingredients 연결이 정확해짐
    missing = [pn for pn in seen if pn not in product_num_to_id]
    if missing:
        with conn.cursor() as cur:
            for i in range(0, len(missing), BATCH_SIZE):
                batch = missing[i : i + BATCH_SIZE]
                cur.execute(
                    "select id, product_num from products where product_num = any(%s);",
                    (batch,),
                )
                for pid, product_num in cur.fetchall():
                    product_num_to_id[product_num] = pid

    return product_num_to_id


def update_flagship_ids(conn, records: list[dict], product_num_to_id: dict):
    """대표상품번호(제품번호 텍스트)를 실제 products.id로 변환해서 flagship_id를 채움"""
    updates = []
    seen = set()
    warnings = []

    for rec in records:
        product_num = normalize(rec.get("제품번호"))
        if not product_num or product_num in seen:
            continue
        seen.add(product_num)

        own_id = product_num_to_id.get(product_num)
        if own_id is None:
            continue  # insert 자체가 안 된 행 (드문 케이스)

        representative_num = normalize(rec.get("대표상품번호")) or product_num
        flagship_id = product_num_to_id.get(representative_num)
        if flagship_id is None:
            warnings.append(product_num)
            flagship_id = own_id

        updates.append((own_id, flagship_id))

    if warnings:
        print(
            f"[경고] 대표상품번호를 찾지 못해 자기 자신으로 대체한 제품 {len(warnings)}건: "
            f"{warnings[:5]}{' ...' if len(warnings) > 5 else ''}"
        )

    update_sql = """
        update products as p
        set flagship_id = v.flagship_id
        from (values %s) as v(id, flagship_id)
        where p.id = v.id
    """
    with conn.cursor() as cur:
        for i in range(0, len(updates), BATCH_SIZE):
            batch = updates[i : i + BATCH_SIZE]
            execute_values(cur, update_sql, batch)
            conn.commit()
            print(f"  [flagship_id] {i + len(batch)}/{len(updates)}건 업데이트 완료")


def build_ingredient_rows(records: list[dict], product_num_to_id: dict,
                           name_to_id: dict, multi_comma_names: set):
    ingredient_rows = []
    unmatched = []
    seen = set()

    for rec in records:
        product_num = normalize(rec.get("제품번호"))
        if not product_num or product_num in seen:
            continue
        seen.add(product_num)

        product_id = product_num_to_id.get(product_num)
        if product_id is None:
            continue

        raw_ingredients = rec.get("성분") or ""
        raw_ingredients = re.sub(r"^\s*(오리지널|번역)\s*", "", raw_ingredients)

        process_ingredient_text(
            product_id, raw_ingredients, name_to_id, multi_comma_names,
            ingredient_rows, unmatched
        )

    return ingredient_rows, unmatched


# ── DB insert (product_ingredients) ─────────────────────────

def insert_product_ingredients(conn, rows: list[tuple]):
    insert_sql = """
        insert into product_ingredients (product_id, ingredient_id, raw_name, order_no)
        values %s
    """
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            execute_values(cur, insert_sql, batch)
            conn.commit()
            print(f"  [product_ingredients] {i + len(batch)}/{len(rows)}건 적재 완료")


def write_unmatched(unmatched: list[tuple]):
    if not unmatched:
        return
    with open(UNMATCHED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["product_id", "raw_name"])
        writer.writerows(unmatched)
    print(f"[알림] 매칭 안 된 {len(unmatched)}건을 {UNMATCHED_CSV}에 저장했어요.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--files", required=True,
        help='정제된 CSV 파일 glob 패턴 또는 쉼표로 구분한 여러 경로'
    )
    args = parser.parse_args()

    db_url = os.getenv("SUPABASE_DB_URL")
    if not db_url:
        print("[에러] .env에 SUPABASE_DB_URL이 없어요.")
        sys.exit(1)

    files = find_csv_files(args.files)
    if not files:
        print(f"[에러] '{args.files}' 패턴에 맞는 파일을 못 찾았어요.")
        sys.exit(1)

    print(f"1) 대상 파일 {len(files)}개: {files}")

    all_records = []
    for path in files:
        recs = load_products_from_csv(path)
        print(f"   {path}: {len(recs)}건")
        all_records.extend(recs)

    print(f"   총 {len(all_records)}건 로드됨")

    print("2) Supabase DB 연결 중...")
    conn = psycopg2.connect(db_url)

    try:
        print("3) products 적재 및 id 확보 중...")
        product_num_to_id = insert_products_and_get_ids(conn, all_records)
        print(f"   products: {len(product_num_to_id)}건 (id 확보 완료)")

        print("4) flagship_id 계산 및 업데이트 중...")
        update_flagship_ids(conn, all_records, product_num_to_id)

        print("5) ingredients + synonyms 이름 매핑 가져오는 중...")
        name_to_id = build_ingredient_name_map(conn)
        print(f"   매칭용 이름 {len(name_to_id)}개 로드됨")

        print("5-1) 콤마 포함된 성분명 사전 가져오는 중...")
        multi_comma_names = build_multi_comma_names(conn)
        print(f"   콤마 포함 성분명 {len(multi_comma_names)}개 로드됨")

        print("6) 성분 데이터 변환 중...")
        ingredient_rows, unmatched = build_ingredient_rows(
            all_records, product_num_to_id, name_to_id, multi_comma_names
        )
        print(f"   product_ingredients: {len(ingredient_rows)}건")
        print(f"   매칭 실패: {len(unmatched)}건")

        write_unmatched(unmatched)

        print("7) product_ingredients 적재...")
        insert_product_ingredients(conn, ingredient_rows)

        print("완료!")
    finally:
        conn.close()


if __name__ == "__main__":
    main()