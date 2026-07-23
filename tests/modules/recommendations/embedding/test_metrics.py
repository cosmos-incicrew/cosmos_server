"""compare_embeddings 의 검색·지표 로직 self-check (모델·DB 불필요)."""

import numpy as np

from tests.modules.recommendations.embedding.compare_embeddings import (
    _norm_name,
    _topk_idx,
    eval_cases,
    eval_efficacy,
    map_ingredients,
)


def _unit(vecs):
    a = np.asarray(vecs, dtype=np.float32)
    return a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)


def test_topk_orders_by_cosine():
    corpus = _unit([[1, 0], [0.9, 0.1], [0, 1]])
    q = _unit([[1, 0]])
    idx = _topk_idx(corpus, q, 2)
    assert idx[0].tolist() == [0, 1]  # 가장 가까운 순


def test_efficacy_recall_and_mrr():
    # 코퍼스 3성분: id 10(정답), 20, 30. 질의는 id10 방향.
    corpus = _unit([[1, 0], [0.5, 0.5], [0, 1]])
    ids = [10, 20, 30]
    q = _unit([[0.9, 0.1]])  # top1=id10
    gt = {"주름": {10}}
    m = eval_efficacy(q, ["주름"], corpus, ids, gt)
    assert m["recall@5"] == 1.0
    assert m["MRR"] == 1.0  # 정답이 1순위

    # 정답을 2순위로: 질의를 id20 쪽으로
    q2 = _unit([[0.5, 0.6]])  # top1=id20(0.5,0.5), top2=id30 or id10?
    gt2 = {"주름": {30}}
    m2 = eval_efficacy(q2, ["주름"], corpus, ids, gt2)
    assert m2["recall@5"] == 1.0
    assert 0 < m2["MRR"] <= 1.0

    # 정답 없음
    m3 = eval_efficacy(q, ["주름"], corpus, ids, {"주름": {999}})
    assert m3["recall@5"] == 0.0 and m3["MRR"] == 0.0


def test_cases_concern_hit():
    # 케이스 코퍼스 4행, concern 라벨.
    corpus = _unit([[1, 0], [0.95, 0.05], [0.9, 0.1], [0, 1]])
    labels = ["홍조", "홍조", "여드름", "주름"]
    q = _unit([[1, 0]])  # top3 = idx0,1,2 → 홍조,홍조,여드름 → 2/3
    m = eval_cases(q, ["홍조"], corpus, labels)
    assert abs(m["concern_hit@3"] - 2 / 3) < 1e-6


def test_norm_and_mapping():
    assert _norm_name("Retinol") == "RETINOL"  # 영문 대문자화
    assert _norm_name("레티놀") == "레티놀"  # 한글 원문
    mp = {"RETINOL": 1, "레티놀": 1, "NIACINAMIDE": 2}
    assert map_ingredients(["Retinol", "니아신아마이드", "niacinamide"], mp) == {1, 2}


if __name__ == "__main__":
    for fn in [
        test_topk_orders_by_cosine,
        test_efficacy_recall_and_mrr,
        test_cases_concern_hit,
        test_norm_and_mapping,
    ]:
        fn()
        print("ok:", fn.__name__)
    print("ALL PASS")
