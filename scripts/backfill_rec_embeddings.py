"""rec_cases · rec_efficacy 임베딩 백필 (설계 확정: gemini-embedding-001 / 1536 / DOCUMENT).

임베딩 모델 비교(tests/modules/recommendations/embedding/) 결과 확정된 설정으로
두 leg 코퍼스를 임베딩해 DB embedding 컬럼을 채운다.

  - rec_efficacy : name_kr + efficacy + product_traits  (성분 사전)
  - rec_cases    : question                              (유사 상담)

임베딩 대상 텍스트 조합은 비교 스크립트(build_*_corpus)와 동일해야 검색 분포가 맞는다.
embed_documents(app/modules/recommendations/embedding.py, task_type=RETRIEVAL_DOCUMENT,
output_dimensionality=settings.embedding_dimensions, L2정규화)를 재사용 — 질의 임베딩과
같은 소스이므로 env(embedding_model/embedding_dimensions) 변경 시 서로 어긋나지 않는다.

실행:  .venv/bin/python -m scripts.backfill_rec_embeddings [efficacy|cases]
       인자 없으면 둘 다. 이미 채워진 행은 건너뛴다(--force 로 전체 재적재).
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.modules.recommendations.embedding import embed_documents, to_pgvector  # noqa: E402

# 비교 스크립트의 검증된 데이터 로더를 그대로 재사용한다(중복 구현 방지).
from tests.modules.recommendations.embedding.compare_embeddings import (  # noqa: E402
    _client,
    _fetch_all,
)

UPDATE_CONCURRENCY = 8


def _efficacy_text(r: dict) -> str:
    parts = [r.get("name_kr") or "", r.get("efficacy") or "", r.get("product_traits") or ""]
    return " ".join(p for p in parts if p).strip()


# (table, pk, select, 텍스트 조합, 스킵 필터) — 두 leg 를 한 경로로 처리
JOBS = {
    "efficacy": {
        "table": "rec_efficacy",
        "pk": "id",
        "select": "id,name_kr,efficacy,product_traits",
        "text": _efficacy_text,
    },
    "cases": {
        "table": "rec_cases",
        "pk": "case_id",
        "select": "case_id,question",
        "text": lambda r: r.get("question") or "",
    },
}


def backfill(sb, job: dict, force: bool) -> None:
    table, pk = job["table"], job["pk"]
    rows = _fetch_all(sb, table, job["select"] + ",embedding")
    todo = [r for r in rows if force or r.get("embedding") is None]
    skipped = len(rows) - len(todo)
    print(f"[{table}] 전체 {len(rows)} · 대상 {len(todo)} · 스킵 {skipped}(이미 채워짐)")
    if not todo:
        return

    texts = [job["text"](r) for r in todo]
    keys = [r[pk] for r in todo]
    print(f"[{table}] 임베딩 계산 중 (gemini-1536, DOCUMENT)…")
    vecs = embed_documents(texts)  # RETRIEVAL_DOCUMENT

    def _update(key, vec) -> None:
        # 대량·장시간 호출 중 supabase HTTP2 연결이 "Server disconnected" 로 끊길 수
        # 있다(일시적). 재시도로 흡수한다 — 없으면 한 행 실패가 전체를 죽인다.
        payload = to_pgvector(vec)
        for attempt in range(5):
            try:
                sb.table(table).update({"embedding": payload}).eq(pk, key).execute()
                return
            except Exception:  # noqa: BLE001 — 연결 오류 백오프 재시도
                if attempt == 4:
                    raise
                time.sleep(2**attempt)

    print(f"[{table}] DB 반영 중…")
    done = 0
    with ThreadPoolExecutor(max_workers=UPDATE_CONCURRENCY) as ex:
        futures = [ex.submit(_update, k, v) for k, v in zip(keys, vecs, strict=True)]
        for fut in as_completed(futures):
            fut.result()
            done += 1
            if done % 500 == 0:
                print(f"  {table} {done}/{len(keys)}")
    print(f"[{table}] 완료 — {len(keys)} 행 적재")


def main(argv: list[str]) -> None:
    force = "--force" in argv
    targets = [a for a in argv if a in JOBS] or list(JOBS)
    print(f"백필 대상: {targets}{' (force)' if force else ''}")

    sb = _client()
    failed = []
    for name in targets:
        try:
            backfill(sb, JOBS[name], force)
        except Exception as e:  # noqa: BLE001 — 한 leg 실패가 다른 leg 를 막지 않게
            print(f"[{name}] 실패: {e!r} — 다음 대상 계속 (재실행하면 남은 행만 채움)")
            failed.append(name)
    if failed:
        print(f"실패한 대상: {failed} — 재실행 시 이미 채워진 행은 자동 스킵")
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
