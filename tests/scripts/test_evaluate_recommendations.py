"""추천 평가 지표 계산·성분명 canonical 매칭 단위 테스트.

파이프라인(DB·LLM)은 태우지 않는다 — recall/hit/mrr 산식과 정답/추천을 접는 매칭만
검증한다. 이 둘이 틀리면 평가 수치 전체가 무의미해진다.
"""

from scripts.evaluate_recommendations import (
    CaseResult,
    _first_hit_rank,
    _hit_at,
    _mrr_at,
    _recall_at_k,
    build_summary,
)
from scripts.load_recommendations_data import build_canonicalizer


def test_recall_at_k_counts_only_top_k() -> None:
    gold = {"콜라겐", "펩타이드", "세라마이드", "황"}
    predicted = ["콜라겐", "오답", "펩타이드", "오답2", "세라마이드"]
    result = _recall_at_k(predicted, gold)
    assert result["@3"] == 2 / 4  # 상위 3개 중 정답 2개(콜라겐·펩타이드)
    assert result["@5"] == 3 / 4  # 상위 5개 중 정답 3개


def test_recall_at_k_empty_gold_is_zero() -> None:
    assert _recall_at_k(["콜라겐"], set()) == {"@3": 0.0, "@5": 0.0}


def test_first_hit_rank_is_one_indexed() -> None:
    assert _first_hit_rank(["오답", "콜라겐", "펩타이드"], {"콜라겐"}) == 2
    assert _first_hit_rank(["오답"], {"콜라겐"}) is None


def test_hit_and_mrr_respect_cutoff() -> None:
    ranks = [1, 3, 6, None]  # 첫 정답 순위들
    assert _hit_at(ranks, 1) == 1 / 4  # rank==1 하나
    assert _hit_at(ranks, 5) == 2 / 4  # rank 1,3 만 5 이하
    assert _mrr_at(ranks, 5) == (1 / 1 + 1 / 3) / 4  # 6·None 은 0


def test_build_summary_separates_top_and_generated() -> None:
    results = [
        CaseResult(
            case_id="c1",
            concern="pores",
            gold_count=2,
            status="ok",
            recall_top={"@3": 0.5, "@5": 0.5},
            recall_generated={"@3": 1.0, "@5": 1.0},
            top_rank=2,
            gen_rank=1,
            covered=True,
        )
    ]
    summary = build_summary(results)
    assert summary["recall"]["top_ingredients"]["@5"] == 0.5
    assert summary["recall"]["recommended_names"]["@5"] == 1.0
    assert summary["hit"]["top_ingredients"]["@1"] == 0.0  # top_rank=2 라 @1 미스
    assert summary["hit"]["recommended_names"]["@1"] == 1.0
    assert summary["rule_compliance"]["concern_coverage_rate"] == 1.0


def test_canonicalizer_folds_english_inci_to_korean() -> None:
    canon = build_canonicalizer(
        [
            {"name_kor": "황", "inci": "SULFUR"},
            {"name_kor": "헥사펩타이드-2", "inci": "Hexapeptide-2"},
        ]
    )
    # 영문 INCI·대소문자·한글이 모두 같은 대표 키로 접힌다
    assert canon("SULFUR") == canon("황") == "황"
    assert canon("Hexapeptide-2") == canon("헥사펩타이드-2") == "헥사펩타이드-2"
    # 사전에 없는 이름은 정규화만 (대문자·공백 정리)
    assert canon("모로칸 용암점토") == "모로칸 용암점토"
