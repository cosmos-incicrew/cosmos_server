"""임베딩 모델 4종 검색 품질 비교 (Notion 스펙: 임베딩 모델 비교).

모델: bge-m3 · gemini-embedding-001(Vertex) · multilingual-e5-large · jina-embeddings-v3

서비스의 두 다리(leg) 병렬 검색을 그대로 재현해 정량 비교한다.
  ① efficacy leg — 고민(concern) → rec_efficacy 성분 사전 검색 (recall@5/@10, MRR)
  ② cases   leg — 사람묘사+고민 → rec_cases 유사 상담 검색 (concern_hit@3)

임베딩은 DB에 저장하지 않고 매 실행 메모리에서 새로 계산한다(모델 간 공정 비교).
결과는 이 폴더에 모델별 CSV + 취합 CSV 로 남긴다.

실행:  .venv/bin/python -m tests.modules.recommendations.embedding.compare_embeddings [모델명 ...]
       모델명을 주면 그 모델만, 안 주면 4종 전부.
"""

from __future__ import annotations

import csv
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

# 서버 설정(.env·Vertex 인증)을 그대로 재사용하려 repo 루트를 path 에 올린다.
# tests/modules/recommendations/embedding/ → 4단계 위가 repo 루트.
_ROOT = Path(__file__).resolve().parents[4]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx 는 임베딩 호출마다 INFO 한 줄을 찍어 로그를 1만+ 줄로 부풀린다 — 경고만 남긴다.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("compare_embeddings")

OUT_DIR = Path(__file__).parent

# ── 테스트 질의 프로필 (3×2×3 = 18) — Notion "테스트 질의 생성 방식" ──────────
AGES = ["10대", "30대", "50대"]
GENDERS = ["여자", "남자"]
SKIN_TYPES = ["건성", "지성", "복합성"]

CASES_TOP_K = 3
EFFICACY_TOP_KS = (5, 10)
EMBED_BATCH = 64

# ── 모델 레지스트리 ───────────────────────────────────────────────────────
# prompt_style: 코퍼스=passage, 질의=query 를 각 모델 규칙대로 처리하는 키.
MODELS: dict[str, dict] = {
    "bge-m3": {"kind": "st", "hf": "BAAI/bge-m3", "prompt_style": "none"},
    "multilingual-e5-large": {
        "kind": "st", "hf": "intfloat/multilingual-e5-large", "prompt_style": "e5",
    },
    "jina-embeddings-v3": {
        "kind": "st", "hf": "jinaai/jina-embeddings-v3", "prompt_style": "jina",
    },
    "gemini-embedding-001": {
        "kind": "gemini", "model": "gemini-embedding-001", "prompt_style": "gemini",
    },
}


# ══════════════════════════════════════════════════════════════════════════
# 1) 데이터 로드 (Supabase, service_role)
# ══════════════════════════════════════════════════════════════════════════
def _client():
    from supabase import create_client

    from app.core.config import get_settings

    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_role_key)


def _fetch_all(sb, table: str, columns: str) -> list[dict]:
    """PostgREST 1000행 제한 → range 페이징으로 전체를 가져온다."""
    out: list[dict] = []
    step = 1000
    start = 0
    while True:
        res = sb.table(table).select(columns).range(start, start + step - 1).execute()
        rows = res.data or []
        out.extend(rows)
        if len(rows) < step:
            return out
        start += step


def _norm_name(name: str) -> str:
    """영문은 대문자로 통일, 한글/기타는 원문 유지 (Notion 정규화 규칙)."""
    n = name.strip()
    if not n:
        return ""
    # 영문자를 포함하면 INCI 로 보고 대문자 통일; 그 외(한글)는 그대로.
    return n.upper() if any(c.isascii() and c.isalpha() for c in n) else n


def load_ingredient_map(sb) -> dict[str, int]:
    """성분명(한/영) → ingredient_id 통합 사전.

    ingredients(name_kor, name_eng) + synonyms(synonym) 를 합친다.
    """
    mapping: dict[str, int] = {}

    def put(name: str | None, iid: int | None) -> None:
        if not name or iid is None:
            return
        key = _norm_name(name)
        if key:
            mapping.setdefault(key, iid)

    for r in _fetch_all(sb, "ingredients", "ingredient_id,name_kor,name_eng"):
        put(r.get("name_kor"), r.get("ingredient_id"))
        put(r.get("name_eng"), r.get("ingredient_id"))
    for r in _fetch_all(sb, "synonyms", "ingredient_id,synonym"):
        put(r.get("synonym"), r.get("ingredient_id"))
    log.info("성분 매핑 사전: %d 개 표제어", len(mapping))
    return mapping


def map_ingredients(names: list[str], mapping: dict[str, int]) -> set[int]:
    """성분명 배열 → ingredient_id 집합 (매핑 실패한 이름은 버린다)."""
    out: set[int] = set()
    for nm in names or []:
        iid = mapping.get(_norm_name(nm))
        if iid is not None:
            out.add(iid)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 2) 코퍼스 · 정답 · 질의
# ══════════════════════════════════════════════════════════════════════════
def build_efficacy_corpus(sb) -> tuple[list[str], list[int | None]]:
    """rec_efficacy: name_kor + efficacy + product_traits 결합 텍스트와 ingredient_id."""
    texts, ids = [], []
    for r in _fetch_all(sb, "rec_efficacy", "ingredient_id,name_kor,efficacy,product_traits"):
        parts = [r.get("name_kor") or "", r.get("efficacy") or "", r.get("product_traits") or ""]
        texts.append(" ".join(p for p in parts if p).strip())
        ids.append(r.get("ingredient_id"))
    log.info("efficacy 코퍼스: %d 행", len(texts))
    return texts, ids


def build_cases_corpus(sb) -> tuple[list[str], list[str]]:
    """rec_cases: question 텍스트와 target_concern."""
    texts, concerns = [], []
    for r in _fetch_all(sb, "rec_cases", "target_concern,question,recommended_ingredients"):
        texts.append(r.get("question") or "")
        concerns.append(r.get("target_concern") or "")
    log.info("cases 코퍼스: %d 행", len(texts))
    return texts, concerns


def build_ground_truth(sb, mapping: dict[str, int]) -> tuple[dict[str, set[int]], list[str]]:
    """concern → 정답 ingredient_id 집합.

    같은 target_concern 을 가진 모든 rec_cases 의 recommended_ingredients 를 union.
    """
    gt: dict[str, set[int]] = defaultdict(set)
    for r in _fetch_all(sb, "rec_cases", "target_concern,recommended_ingredients"):
        concern = r.get("target_concern") or ""
        if not concern:
            continue
        gt[concern] |= map_ingredients(r.get("recommended_ingredients") or [], mapping)
    concerns = sorted(gt)
    for c in concerns:
        log.info("정답 concern=%s → %d 성분", c, len(gt[c]))
    return dict(gt), concerns


def build_query_text(concern: str, age: str, gender: str, skin: str) -> str:
    """cases leg 질의: 사람 묘사 + 고민 (예: '홍조 30대 여자 건성')."""
    return f"{concern} {age} {gender} {skin}"


def build_queries(concerns: list[str]) -> tuple[list[dict], list[str]]:
    """cases 질의(concern×프로필 18) 와 efficacy 질의(concern 자체)."""
    cases_q = []
    for c in concerns:
        for age in AGES:
            for g in GENDERS:
                for sk in SKIN_TYPES:
                    cases_q.append({"concern": c, "text": build_query_text(c, age, g, sk)})
    efficacy_q = list(concerns)  # 고민 값 그 자체
    log.info("질의: cases=%d, efficacy=%d", len(cases_q), len(efficacy_q))
    return cases_q, efficacy_q


# ══════════════════════════════════════════════════════════════════════════
# 3) 임베더 (모델별 prompt 규칙 적용) — encode(texts, is_query) -> (N, D) L2정규화
# ══════════════════════════════════════════════════════════════════════════
class STEmbedder:
    """sentence-transformers 기반 로컬 모델 (bge-m3 · e5 · jina-v3)."""

    def __init__(self, hf: str, prompt_style: str):
        from sentence_transformers import SentenceTransformer

        self.style = prompt_style
        kw = {"trust_remote_code": True} if prompt_style == "jina" else {}
        self.model = SentenceTransformer(hf, **kw)

    def encode(self, texts: list[str], is_query: bool) -> np.ndarray:
        if self.style == "e5":  # "query: " / "passage: " 접두어
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
            return self._enc(texts)
        if self.style == "jina":  # task 파라미터로 구분
            task = "retrieval.query" if is_query else "retrieval.passage"
            return self._enc(texts, task=task)
        return self._enc(texts)  # bge-m3: 원문 그대로

    def _enc(self, texts: list[str], **kw) -> np.ndarray:
        return self.model.encode(
            texts,
            batch_size=EMBED_BATCH,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=True,
            **kw,
        )


GEMINI_CONCURRENCY = 8  # Vertex rate limit 여유선. 429 나면 백오프가 자동 흡수한다.
# native 는 3072 지만 로컬 3종(1024)과 차원을 맞춰 공정 비교한다. 3072 미만으로
# 절단하면 SDK 가 정규화하지 않으므로 encode() 의 L2 정규화가 필수다.
GEMINI_OUTPUT_DIM = 1536


class GeminiEmbedder:
    """Vertex 경유 gemini-embedding-001. task_type 으로 query/document 구분.

    요청당 1 인스턴스만 허용해 건수가 곧 호출수다(1만+). 순차면 ~80분이라
    ThreadPool 로 병렬 호출하되 결과는 원래 순서로 되돌린다.
    """

    def __init__(self, model: str):
        from app.core.gemini import get_gemini

        self.client = get_gemini()
        self.model = model

    def _one(self, text: str, task: str) -> list[float]:
        from google.genai import types

        for attempt in range(5):
            try:
                res = self.client.models.embed_content(
                    model=self.model,
                    contents=text or " ",
                    config=types.EmbedContentConfig(
                        task_type=task, output_dimensionality=GEMINI_OUTPUT_DIM
                    ),
                )
                return res.embeddings[0].values
            except Exception as e:  # noqa: BLE001 — 429/일시 오류는 백오프 재시도
                wait = 2**attempt
                log.warning("gemini embed 실패(%d/5) %s → %ds 후 재시도", attempt + 1, e, wait)
                time.sleep(wait)
        raise RuntimeError("gemini embed 연속 실패")

    def encode(self, texts: list[str], is_query: bool) -> np.ndarray:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        vecs: list[list[float] | None] = [None] * len(texts)
        done = 0
        with ThreadPoolExecutor(max_workers=GEMINI_CONCURRENCY) as ex:
            futures = {ex.submit(self._one, t, task): i for i, t in enumerate(texts)}
            for fut in as_completed(futures):
                vecs[futures[fut]] = fut.result()
                done += 1
                if done % 500 == 0:
                    log.info("  gemini %d/%d", done, len(texts))
        arr = np.asarray(vecs, dtype=np.float32)
        arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9  # 코사인용 정규화
        return arr


def make_embedder(name: str):
    cfg = MODELS[name]
    if cfg["kind"] == "gemini":
        return GeminiEmbedder(cfg["model"])
    return STEmbedder(cfg["hf"], cfg["prompt_style"])


# ══════════════════════════════════════════════════════════════════════════
# 4) 검색 · 지표
# ══════════════════════════════════════════════════════════════════════════
def _topk_idx(corpus: np.ndarray, queries: np.ndarray, k: int) -> np.ndarray:
    """코사인 유사도 top-k 인덱스 (queries: (Q,D), corpus: (N,D)) → (Q,k)."""
    sims = queries @ corpus.T  # 둘 다 정규화돼 있어 dot = cosine
    k = min(k, sims.shape[1])
    part = np.argpartition(-sims, kth=k - 1, axis=1)[:, :k]
    # 부분정렬 결과를 유사도 내림차순으로 재정렬
    order = np.argsort(-np.take_along_axis(sims, part, axis=1), axis=1)
    return np.take_along_axis(part, order, axis=1)


def eval_efficacy(
    q_emb: np.ndarray,
    concerns: list[str],
    corpus_emb: np.ndarray,
    corpus_ids: list[int | None],
    gt: dict[str, set[int]],
) -> dict[str, float]:
    """efficacy leg: recall@5, recall@10, MRR (concern 단위 평균)."""
    max_k = max(EFFICACY_TOP_KS)
    top = _topk_idx(corpus_emb, q_emb, max_k)
    recalls = {k: [] for k in EFFICACY_TOP_KS}
    rr = []
    for qi, concern in enumerate(concerns):
        answer = gt.get(concern, set())
        ranked_ids = [corpus_ids[i] for i in top[qi]]
        for k in EFFICACY_TOP_KS:
            hit = any(iid in answer for iid in ranked_ids[:k])
            recalls[k].append(1.0 if hit else 0.0)
        # MRR: 정답 성분이 처음 등장한 순위의 역수
        rank = next((r for r, iid in enumerate(ranked_ids, 1) if iid in answer), None)
        rr.append(1.0 / rank if rank else 0.0)
    return {
        "recall@5": float(np.mean(recalls[5])),
        "recall@10": float(np.mean(recalls[10])),
        "MRR": float(np.mean(rr)),
    }


def eval_cases(
    q_emb: np.ndarray,
    q_concerns: list[str],
    corpus_emb: np.ndarray,
    corpus_concerns: list[str],
) -> dict[str, float]:
    """cases leg: concern_hit@3 — top-3 케이스의 target_concern 일치 비율."""
    top = _topk_idx(corpus_emb, q_emb, CASES_TOP_K)
    hits = []
    for qi, concern in enumerate(q_concerns):
        got = [corpus_concerns[i] for i in top[qi]]
        hits.append(sum(1 for c in got if c == concern) / len(got))
    return {"concern_hit@3": float(np.mean(hits))}


# ══════════════════════════════════════════════════════════════════════════
# 5) 모델 1개 평가
# ══════════════════════════════════════════════════════════════════════════
def evaluate_model(
    name: str,
    eff_texts: list[str],
    eff_ids: list[int | None],
    case_texts: list[str],
    case_concerns: list[str],
    gt: dict[str, set[int]],
    concerns: list[str],
    cases_q: list[dict],
    efficacy_q: list[str],
) -> tuple[dict, list[dict]]:
    log.info("═══ 모델 평가 시작: %s ═══", name)
    t0 = time.time()
    emb = make_embedder(name)

    log.info("[%s] efficacy 코퍼스 임베딩", name)
    eff_corpus = emb.encode(eff_texts, is_query=False)
    log.info("[%s] cases 코퍼스 임베딩", name)
    case_corpus = emb.encode(case_texts, is_query=False)
    log.info("[%s] 질의 임베딩", name)
    eff_q_emb = emb.encode(efficacy_q, is_query=True)
    case_q_emb = emb.encode([q["text"] for q in cases_q], is_query=True)

    overall = {
        **eval_efficacy(eff_q_emb, efficacy_q, eff_corpus, eff_ids, gt),
        **eval_cases(case_q_emb, [q["concern"] for q in cases_q], case_corpus, case_concerns),
    }
    overall = {"model": name, **{k: round(v, 4) for k, v in overall.items()}}

    # concern 별 상세 (efficacy 지표 + 해당 concern 프로필들의 cases hit)
    by_concern = []
    case_top = _topk_idx(case_corpus, case_q_emb, CASES_TOP_K)
    for ci, concern in enumerate(concerns):
        eff_row = eval_efficacy(
            eff_q_emb[ci : ci + 1], [concern], eff_corpus, eff_ids, gt
        )
        idxs = [i for i, q in enumerate(cases_q) if q["concern"] == concern]
        chit = float(
            np.mean(
                [
                    sum(1 for j in case_top[i] if case_concerns[j] == concern) / CASES_TOP_K
                    for i in idxs
                ]
            )
        )
        by_concern.append(
            {
                "model": name,
                "concern": concern,
                "n_answer_ings": len(gt.get(concern, set())),
                **{k: round(v, 4) for k, v in eff_row.items()},
                "concern_hit@3": round(chit, 4),
            }
        )
    log.info("═══ %s 완료 (%.1fs): %s ═══", name, time.time() - t0, overall)
    return overall, by_concern


# ══════════════════════════════════════════════════════════════════════════
# 6) 저장
# ══════════════════════════════════════════════════════════════════════════
def _collect_model_csvs(pattern: str) -> list[dict]:
    """폴더의 모델별 CSV(파일명에 `_모델명` 접미) 를 모두 읽어 한 목록으로 합친다.

    취합 결과물(`*_summary.csv` 자신 등 접미 없는 파일)은 제외한다.
    """
    rows: list[dict] = []
    base = pattern.replace("_*.csv", ".csv")  # 취합 파일 자신
    for path in sorted(OUT_DIR.glob(pattern)):
        if path.name == base:
            continue
        with path.open(encoding="utf-8-sig") as f:
            rows.extend(csv.DictReader(f))
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("저장: %s", path.relative_to(_ROOT))


def main(argv: list[str]) -> None:
    targets = [a for a in argv if a in MODELS] or list(MODELS)
    log.info("비교 대상 모델: %s", targets)

    sb = _client()
    mapping = load_ingredient_map(sb)
    eff_texts, eff_ids = build_efficacy_corpus(sb)
    case_texts, case_concerns = build_cases_corpus(sb)
    gt, concerns = build_ground_truth(sb, mapping)
    cases_q, efficacy_q = build_queries(concerns)

    for name in targets:
        try:
            summary, by_concern = evaluate_model(
                name, eff_texts, eff_ids, case_texts, case_concerns,
                gt, concerns, cases_q, efficacy_q,
            )
        except Exception:
            log.exception("[%s] 평가 실패 — 건너뜀", name)
            continue
        _write_csv(OUT_DIR / f"embedding_comparison_summary_{name}.csv", [summary])
        _write_csv(OUT_DIR / f"embedding_comparison_by_concern_{name}.csv", by_concern)

    # 취합 CSV 는 이번 실행분이 아니라 폴더의 모든 모델별 파일을 읽어 만든다
    # (모델을 나눠 실행해도 전체가 한 표에 모이도록 — Notion "자동 취합" 규칙).
    merged_summary = _collect_model_csvs("embedding_comparison_summary_*.csv")
    merged_by_concern = _collect_model_csvs("embedding_comparison_by_concern_*.csv")
    _write_csv(OUT_DIR / "embedding_comparison_summary.csv", merged_summary)
    _write_csv(OUT_DIR / "embedding_comparison_by_concern.csv", merged_by_concern)

    # 콘솔 요약 표
    if merged_summary:
        cols = ["model", "recall@5", "recall@10", "MRR", "concern_hit@3"]

        def _fmt(v: object, c: str) -> str:
            return f"{v:>22}" if c == "model" else f"{v:>12}"

        print("\n" + " | ".join(_fmt(c, c) for c in cols))
        for r in merged_summary:
            print(" | ".join(_fmt(r.get(c, ""), c) for c in cols))


if __name__ == "__main__":
    main(sys.argv[1:])
