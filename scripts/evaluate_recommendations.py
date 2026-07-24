"""고정 held-out 데이터셋으로 추천 파이프라인 자체의 품질을 측정한다.

검색 평가(evaluate_ingredient_search.py)와 같은 골격 — 버전 데이터셋 → 실제 실행 →
지표 집계. 다만 대상이 검색이 아니라 ②~⑩ 전체라 실 DB + Vertex(임베딩·생성)가 필요하다.

지표 두 갈래:
  · recall@k — 파이프라인 추천 ∩ Validation 정답(한글 성분명). 두 출력에 각각 잰다:
      top_ingredients(메인 카드, 응답 노출) / recommended_names(⑥ 서사, 내부값).
      recommended_names 는 RecommendationResponse 에 없어 ⑥ generate 를 래핑해 캡처한다.
  · 규칙 준수(결정적) — 고민 커버리지 · 금칙어(화장품법 §13) · insufficient 비율.
      금지 성분 제외는 ⑤가 구조적으로 보장하므로(재조회 없이) 여기선 검증 대상이 아니다.

BSTI 없이 태운다(Validation 에 BSTI 축이 없음) — ⑦ BSTI 가지는 꺼지고 고민 축만 평가된다.

환경: .env(SUPABASE_*, GCP_*) + GOOGLE_APPLICATION_CREDENTIALS 를 프로세스 env 로 export.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from app.modules.recommendations import service
from app.modules.recommendations.constants import BANNED_CLAIM_TERMS
from app.modules.recommendations.pipeline import s6_generation
from app.modules.recommendations.schemas import LlmNarrative, UserContext
from scripts.load_recommendations_data import build_canonicalizer, read_ingredients
from scripts.recommendations_dataset import DEFAULT_SRC

DEFAULT_DATASET = Path("evaluation/recommendations/datasets/held-out-v1.0.0.json")
DEFAULT_OUTPUT = Path("artifacts/recommendations-evaluation/held-out.json")
RECALL_KS = (3, 5)

# 정답·추천을 같은 대표 한글명으로 접어 매칭하는 함수 (main 에서 성분 사전으로 교체).
def _canon(name: str) -> str:
    return name


@dataclass
class CaseResult:
    case_id: str
    concern: str
    gold_count: int
    status: str
    top_names: list[str] = field(default_factory=list)
    recommended_names: list[str] = field(default_factory=list)
    recall_top: dict[str, float] = field(default_factory=dict)
    recall_generated: dict[str, float] = field(default_factory=dict)
    top_rank: int | None = None  # 첫 정답의 순위(1-indexed) — hit@k·mrr 용
    gen_rank: int | None = None
    covered: bool = False
    banned_claim_hit: bool = False
    latency_ms: float = 0.0
    error: str | None = None


def _norm_set(names: list[str]) -> list[str]:
    """대표 한글명(canonical) + 순서 보존 dedupe (recall@k 는 순서에 의존)."""
    out: list[str] = []
    for n in names:
        key = _canon(n)
        if key and key not in out:
            out.append(key)
    return out


def _recall_at_k(predicted: list[str], gold: set[str]) -> dict[str, float]:
    if not gold:
        return {f"@{k}": 0.0 for k in RECALL_KS}
    return {
        f"@{k}": len(set(predicted[:k]) & gold) / len(gold) for k in RECALL_KS
    }


def _first_hit_rank(predicted: list[str], gold: set[str]) -> int | None:
    """첫 정답의 순위(1-indexed). 없으면 None. hit@k·mrr 의 공통 입력."""
    return next((i for i, name in enumerate(predicted, 1) if name in gold), None)


def _has_banned_claim(answer: Any) -> bool:
    if answer is None:
        return False
    text = " ".join([answer.cause_analysis, answer.recommendation, answer.usage_guide])
    return any(term in text for term in BANNED_CLAIM_TERMS)


def _install_narrative_capture() -> dict[str, LlmNarrative | None]:
    """⑥ generate 를 래핑해 마지막 narrative(recommended_names 보유)를 캡처한다.

    RecommendationResponse 에 recommended_names 가 없어 응답만으로는 잴 수 없다.
    파이프라인은 건드리지 않고 평가 프로세스 안에서만 모듈 속성을 바꾼다.
    """
    holder: dict[str, LlmNarrative | None] = {"narrative": None}
    original = s6_generation.generate

    async def wrapped(*args: Any, **kwargs: Any) -> LlmNarrative:
        narrative = await original(*args, **kwargs)
        holder["narrative"] = narrative
        return narrative

    s6_generation.generate = wrapped  # type: ignore[assignment]
    service.s6_generation.generate = wrapped  # type: ignore[assignment]
    return holder


async def _run_case(case: dict[str, Any], holder: dict[str, LlmNarrative | None]) -> CaseResult:
    gold = set(_norm_set(case["gold"]))
    result = CaseResult(
        case_id=case["case_id"], concern=case["concern"], gold_count=len(gold), status="?"
    )
    holder["narrative"] = None
    ctx = UserContext(
        user_id=f"eval-{case['case_id']}",
        age=case.get("age"),
        gender=case.get("gender"),
        concerns=[case["concern"]],
    )
    started = perf_counter()
    try:
        resp = await service.run_for_context(ctx)
    except Exception as exc:  # 개별 케이스 실패는 집계에서 격리
        result.error = type(exc).__name__
        result.status = "error"
        result.latency_ms = (perf_counter() - started) * 1_000
        return result
    result.latency_ms = (perf_counter() - started) * 1_000
    result.status = resp.status
    result.top_names = _norm_set([i.name_kor for i in resp.top_ingredients])
    narrative = holder["narrative"]
    result.recommended_names = _norm_set(narrative.recommended_names) if narrative else []
    result.recall_top = _recall_at_k(result.top_names, gold)
    result.recall_generated = _recall_at_k(result.recommended_names, gold)
    result.top_rank = _first_hit_rank(result.top_names, gold)
    result.gen_rank = _first_hit_rank(result.recommended_names, gold)
    result.covered = resp.status == "ok" and bool(resp.top_ingredients)
    result.banned_claim_hit = _has_banned_claim(resp.answer)
    return result


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _hit_at(ranks: list[int | None], k: int) -> float:
    return _mean([float(r is not None and r <= k) for r in ranks])


def _mrr_at(ranks: list[int | None], k: int) -> float:
    return _mean([(1.0 / r) if r is not None and r <= k else 0.0 for r in ranks])


def build_summary(results: list[CaseResult]) -> dict[str, Any]:
    ok = [r for r in results if r.error is None]
    scored = [r for r in ok if r.status == "ok"]
    latencies = [r.latency_ms for r in ok]
    top_ranks = [r.top_rank for r in scored]
    gen_ranks = [r.gen_rank for r in scored]

    def _recall_mean(pick: str, k: int) -> float:
        return round(_mean([getattr(r, pick)[f"@{k}"] for r in scored]), 4)

    return {
        "case_count": len(results),
        "scored_count": len(scored),
        "error_count": sum(r.error is not None for r in results),
        "insufficient_rate": _mean([float(r.status == "insufficient_evidence") for r in ok]),
        "recall": {
            "top_ingredients": {f"@{k}": _recall_mean("recall_top", k) for k in RECALL_KS},
            "recommended_names": {f"@{k}": _recall_mean("recall_generated", k) for k in RECALL_KS},
        },
        "hit": {
            "top_ingredients": {f"@{k}": round(_hit_at(top_ranks, k), 4) for k in (1, *RECALL_KS)},
            "recommended_names": {
                f"@{k}": round(_hit_at(gen_ranks, k), 4) for k in (1, *RECALL_KS)
            },
        },
        "mrr": {
            "top_ingredients": {"@5": round(_mrr_at(top_ranks, 5), 4)},
            "recommended_names": {"@5": round(_mrr_at(gen_ranks, 5), 4)},
        },
        "rule_compliance": {
            "concern_coverage_rate": round(_mean([float(r.covered) for r in ok]), 4),
            "banned_claim_violations": sum(r.banned_claim_hit for r in scored),
            "banned_claim_case_ids": [r.case_id for r in scored if r.banned_claim_hit],
        },
        "recall_by_concern": {
            concern: round(
                _mean([r.recall_top["@5"] for r in scored if r.concern == concern]), 4
            )
            for concern in sorted({r.concern for r in scored})
        },
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 1),
            "p95": round(_percentile(latencies, 95), 1),
        },
    }


async def _run(dataset: dict[str, Any], limit: int | None) -> dict[str, Any]:
    holder = _install_narrative_capture()
    cases = dataset["cases"][:limit] if limit else dataset["cases"]
    results: list[CaseResult] = []
    for index, case in enumerate(cases, 1):
        results.append(await _run_case(case, holder))
        if index % 10 == 0:
            print(f"  {index}/{len(cases)} …", flush=True)
    return {
        "dataset_version": dataset.get("dataset_version"),
        "dataset_kind": dataset.get("dataset_kind"),
        "summary": build_summary(results),
        "cases": [asdict(r) for r in results],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="held-out 데이터셋 기반 추천 품질 평가")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        raise SystemExit(
            "GOOGLE_APPLICATION_CREDENTIALS 를 export 하세요 (Vertex 임베딩·생성 인증)."
        )
    args = _parse_args()
    global _canon
    _canon = build_canonicalizer(read_ingredients(DEFAULT_SRC))
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = asyncio.run(_run(dataset, args.limit))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"상세 결과: {args.output}")


if __name__ == "__main__":
    main()
