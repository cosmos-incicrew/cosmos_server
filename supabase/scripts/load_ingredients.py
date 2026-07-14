"""
Step 1: kcia_ingredients.json -> Supabase ingredients 테이블 기본 필드 적재

INGR_CODE를 그대로 ingredient_id로 사용함 (SERIAL이라 명시적 값 삽입 가능).

원본 필드명: INGR_CODE, NAME_KOR, NAME_ENG, ORIGIN_DEFINITION, PURPOSE_FORMULATION, CAS_NO
넣는 컬럼: ingredient_id, name_kor, name_eng, cas_no, origin_definition, purpose_formulation

사용법:
    1. .env 파일에 SUPABASE_DB_URL 설정
    2. python load_ingredients_step1.py --file kcia_ingredients.json
"""

import argparse
import json
import os
import sys

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = 1000


def load_json(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        for key in ("data", "items", "records"):
            if key in data and isinstance(data[key], list):
                return data[key]
        raise ValueError(
            "JSON 최상위가 dict인데 list를 못 찾았어요. 최상위 구조를 확인해주세요."
        )

    if not isinstance(data, list):
        raise ValueError(f"JSON 최상위가 list가 아니에요: {type(data)}")

    return data


def normalize(value):
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "" or stripped == "0":
            return None
        return stripped
    return value


def transform(records: list[dict]) -> list[tuple]:
    rows = []
    skipped = 0

    for rec in records:
        name_kor = rec.get("NAME_KOR")
        name_kor = name_kor.strip() if isinstance(name_kor, str) else name_kor

        ingredient_id = rec.get("INGR_CODE")

        if not name_kor or ingredient_id is None:
            skipped += 1
            continue

        name_eng = normalize(rec.get("NAME_ENG"))
        cas_no = normalize(rec.get("CAS_NO"))
        origin_definition = normalize(rec.get("ORIGIN_DEFINITION"))
        purpose_formulation = normalize(rec.get("PURPOSE_FORMULATION"))

        rows.append((
            ingredient_id, name_kor, name_eng, cas_no, origin_definition, purpose_formulation
        ))

    if skipped:
        print(f"[경고] NAME_KOR 또는 INGR_CODE 없는 행 {skipped}건은 건너뛰었어요.")

    return rows


def insert_rows(conn, rows: list[tuple]):
    insert_sql = """
        insert into ingredients
            (ingredient_id, name_kor, name_eng, cas_no, origin_definition, purpose_formulation)
        overriding system value
        values %s
    """

    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            execute_values(cur, insert_sql, batch)
            conn.commit()
            print(f"  {i + len(batch)}/{len(rows)}건 적재 완료")


def reset_identity_sequence(conn):
    """ingredient_id를 명시적으로 넣었으니, 다음 auto-increment가 겹치지 않게
    시퀀스를 현재 최대값 다음으로 맞춰줌"""
    with conn.cursor() as cur:
        cur.execute("""
            select setval(
                pg_get_serial_sequence('ingredients', 'ingredient_id'),
                coalesce((select max(ingredient_id) from ingredients), 1)
            );
        """)
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="kcia_ingredients.json 경로")
    args = parser.parse_args()

    db_url = os.getenv("SUPABASE_DB_URL")
    if not db_url:
        print("[에러] .env에 SUPABASE_DB_URL이 없어요.")
        sys.exit(1)

    print(f"1) {args.file} 읽는 중...")
    records = load_json(args.file)
    print(f"   총 {len(records)}건 로드됨")

    print("2) 데이터 변환 중...")
    rows = transform(records)
    print(f"   {len(rows)}건 insert 준비 완료")

    print("3) Supabase DB 연결 중...")
    conn = psycopg2.connect(db_url)

    try:
        print("4) 적재 시작...")
        insert_rows(conn, rows)
        print("5) identity 시퀀스 리셋 중...")
        reset_identity_sequence(conn)
        print("완료!")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
