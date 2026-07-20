"""
Step: standard_names_list.json -> synonyms 테이블 적재

흐름:
  1. ingredients 테이블에서 (name_kr -> ingredient_id) 매핑을 미리 가져옴
  2. JSON의 STD_NAME_KOR로 매칭해서 ingredient_id 찾기
  3. 매칭되면:
       - OLD_NAME_KOR_LIST의 각 값 -> synonyms row (language='kor')
       - OLD_NAME_ENG_LIST의 각 값 -> synonyms row (language='eng')
       - name_kor 컬럼에는 STD_NAME_KOR을 그대로 저장
  4. 매칭 안 되면 skip하고 unmatched_std_names.csv에 따로 기록

사용법:
    python load_synonyms.py --file standard_names_list.json
"""

import argparse
import csv
import json
import os
import sys

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

BATCH_SIZE = 1000
UNMATCHED_CSV = "unmatched_std_names.csv"


def load_json(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        for key in ("data", "items", "records"):
            if key in data and isinstance(data[key], list):
                return data[key]
        raise ValueError("JSON 최상위가 dict인데 list를 못 찾았어요.")

    if not isinstance(data, list):
        raise ValueError(f"JSON 최상위가 list가 아니에요: {type(data)}")

    return data


def clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v if v else None
    return value


def build_name_to_id_map(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select ingredient_id, name_kr from ingredients where name_kr is not null;")
        rows = cur.fetchall()

    mapping = {}
    dup_names = set()
    for ingredient_id, name_kr in rows:
        key = name_kr.strip()
        if key in mapping:
            dup_names.add(key)
        mapping[key] = ingredient_id

    if dup_names:
        print(
            f"[경고] ingredients.name_kr에 중복된 이름 {len(dup_names)}건 있음"
            f"(마지막 값으로 덮어씀): "
            f"{list(dup_names)[:5]}{' ...' if len(dup_names) > 5 else ''}"
        )

    return mapping


def transform(records: list[dict], name_to_id: dict):
    rows = []  # (ingredient_id, name_kor, synonym, language)
    unmatched = []  # STD_NAME_KOR 값들

    for rec in records:
        std_name_kor = clean(rec.get("STD_NAME_KOR"))
        if not std_name_kor:
            continue

        ingredient_id = name_to_id.get(std_name_kor)
        if ingredient_id is None:
            unmatched.append(std_name_kor)
            continue

        kor_list = rec.get("OLD_NAME_KOR_LIST") or []
        eng_list = rec.get("OLD_NAME_ENG_LIST") or []

        for name in kor_list:
            name = clean(name)
            if name:
                rows.append((ingredient_id, std_name_kor, name, "kor"))

        for name in eng_list:
            name = clean(name)
            if name:
                rows.append((ingredient_id, std_name_kor, name, "eng"))

    return rows, unmatched


def insert_rows(conn, rows: list[tuple]):
    insert_sql = """
        insert into synonyms (ingredient_id, name_kor, synonym, language)
        values %s
    """
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            execute_values(cur, insert_sql, batch)
            conn.commit()
            print(f"  {i + len(batch)}/{len(rows)}건 적재 완료")


def write_unmatched(unmatched: list[str]):
    if not unmatched:
        return
    with open(UNMATCHED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["STD_NAME_KOR"])
        for name in unmatched:
            writer.writerow([name])
    print(f"[알림] 매칭 안 된 {len(unmatched)}건을 {UNMATCHED_CSV}에 저장했어요.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="standard_names_list.json 경로")
    args = parser.parse_args()

    db_url = os.getenv("SUPABASE_DB_URL")
    if not db_url:
        print("[에러] .env에 SUPABASE_DB_URL이 없어요.")
        sys.exit(1)

    print(f"1) {args.file} 읽는 중...")
    records = load_json(args.file)
    print(f"   총 {len(records)}건 로드됨")

    print("2) Supabase DB 연결 중...")
    conn = psycopg2.connect(db_url)

    try:
        print("3) ingredients name_kr -> ingredient_id 매핑 가져오는 중...")
        name_to_id = build_name_to_id_map(conn)
        print(f"   {len(name_to_id)}개 이름 매핑 로드됨")

        print("4) 매칭 및 변환 중...")
        rows, unmatched = transform(records, name_to_id)
        print(f"   synonyms insert 대상: {len(rows)}건")
        print(f"   매칭 실패: {len(unmatched)}건")

        write_unmatched(unmatched)

        print("5) synonyms 테이블에 적재 시작...")
        insert_rows(conn, rows)
        print("완료!")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
