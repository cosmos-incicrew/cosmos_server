# 임베딩 모델 4종 비교

- **작성일**: 2026-07-23
- **작성자**: 민경
- **스펙 출처**: Notion "임베딩 모델 비교"
- **실행 환경**: Apple M4 Pro (MPS), Python 3.12, transformers 4.49 / sentence-transformers 5.6

## 최종 결과 — 채택 구성 (`embedding_comparison_summary.csv`)

로컬 3종은 native 1024차원, gemini 는 서버 스키마(`vector(1536)`)에 맞춘 1536차원.
동일 1024 조건 공정 비교는 아래 "gemini 차원별" 표 참고.

| 모델 | 차원 | recall@5 | recall@10 | MRR | concern_hit@3 |
|---|---|---|---|---|---|
| **gemini-embedding-001** (Vertex) | 1536 | **0.750** | **0.875** | **0.351** | 0.880 |
| multilingual-e5-large | 1024 | 0.375 | 0.625 | 0.225 | 0.750 |
| bge-m3 | 1024 | 0.250 | 0.500 | 0.283 | 0.875 |
| jina-embeddings-v3 | 1024 | 0.250 | 0.375 | 0.174 | **0.931** |

- `recall@k`·`MRR` = **efficacy leg**(고민→성분 사전 검색). 값이 클수록 좋음.
- `concern_hit@3` = **cases leg**(유사 상담 검색). top-3 상담의 target_concern 일치율.

### gemini 차원별 (채택 차원 결정)

| 차원 | recall@5 | recall@10 | MRR | concern_hit@3 | 비고 |
|---|---|---|---|---|---|
| 3072 (native) | 0.750 | 0.875 | 0.323 | 0.884 | 저장 2배·검색 부담 |
| **1536 (채택)** | 0.750 | 0.875 | **0.351** | 0.880 | 서버 스키마 일치, recall 최고치 유지 |
| 1024 (절단) | 0.625 | 0.750 | 0.335 | 0.863 | recall 하락 |

**1536이 최적점** — recall 은 3072와 동일한 최고치(0.875)를 유지하면서 MRR 은 전 차원 중 최고.
1024로 낮춰 로컬 3종과 공정 비교해도 gemini 는 여전히 앞선다(recall@10 0.75 vs e5 0.625).

## 해석

- **efficacy leg(성분 검색)은 gemini-embedding-001 압도적 우위**. 채택 구성(1536)에서 recall@10
  0.875·MRR 0.351 로 2위 e5(0.625/0.225) 대비 큰 격차. 추천 품질의 핵심이 "고민→성분" 매칭이므로
  채택 근거가 강하다.
- **cases leg(유사 상담)는 모델 간 차이가 작다**(0.75~0.93). 상담 질의는 concern 문자열이
  그대로 들어가 어느 모델이든 잘 맞아, 변별력이 낮은 지표다.
- 로컬 3종 중에서는 e5 가 efficacy 에서, jina 가 cases 에서 상대적으로 낫다.

## ⚠️ 해석 시 유의 (표본 한계)

- **efficacy 질의가 concern 8개뿐** → recall 은 0.125 단위로만 움직여 해상도가 거칠다.
  방향성(gemini 우위)은 뚜렷하나 절대 수치는 표본이 작다.
- 정답(ground truth)은 concern별 rec_cases.recommended_ingredients 합집합 → ingredient_id
  변환. 정답 성분은 8개 concern 모두 efficacy 코퍼스에 100% 존재함을 확인(recall 상한 정상).

## 재현

```bash
# 로컬 모델용 의존성 (서버 런타임 아님 — 실험 전용)
uv pip install numpy sentence-transformers einops "transformers>=4.43,<4.50"
# jina-embeddings-v3 는 transformers<4.50 필수 (최신 버전은 remote code 비호환)

# 전체 4종 (첫 실행 ~45분: 로컬 다운로드 5.6GB + gemini 병렬 ~30분)
.venv/bin/python -m tests.modules.recommendations.embedding.compare_embeddings
# 특정 모델만
.venv/bin/python -m tests.modules.recommendations.embedding.compare_embeddings gemini-embedding-001

# 지표 로직 self-check (모델·DB 불필요)
.venv/bin/python -m tests.modules.recommendations.embedding.test_metrics
```

## 산출물

- `embedding_comparison_summary.csv` — 4종 요약(위 표)
- `embedding_comparison_by_concern.csv` — 8개 고민 × 4모델 상세
- `embedding_comparison_{summary,by_concern}_{모델}.csv` — 모델별 개별 파일

---

**구성 근거**: 서비스의 두 다리(efficacy·cases) 병렬 검색을 그대로 재현해, 실제 추천
파이프라인에서의 검색 품질을 측정했다. 임베딩은 DB에 저장하지 않고 매 실행 새로 계산했다.
모델 선택은 1024 통일 공정 비교로, 차원 결정은 gemini 차원별(1024/1536/3072) 비교로 각각
근거를 세워 gemini-embedding-001 · 1536 을 확정했다. 질의 표본 확대(concern 8개 → 프로필별
정답 분리 등)는 후속 과제로 남겼다(위 유의 참고).
