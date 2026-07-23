# 추천 품질 개선 설계서 (데모 피드백 1~7)

- **작성일**: 2026-07-23
- **작성자**: 김민경
- **대상 모듈**: `app/modules/recommendations` · 엔드포인트: `POST /api/v1/recommendations`
- **관계**: [01-recommendations-pipeline.md](01-recommendations-pipeline.md) 파이프라인의 품질 개선. 벡터 검색은 [03-vector-retrieval.md](03-vector-retrieval.md).

## 0. 배경

실 DB 데모(30대 여성·BSTI DSPW·고민 redness+brightening)에서 응답 품질 문제 7건이 관찰됐다. 근본 원인은 세 갈래다:

1. **질의 편향** — cases leg 질의가 "사람묘사(BSTI 4축) + 고민"이라, DSPW의 색소침착(P) 묘사가 질의를 미백 쪽으로 끌어당겨 붉어짐(redness) 케이스를 밀어냈다. (bsti_type=None이던 다른 유저는 redness 케이스가 정상 회수됨)
2. **후보 정책** — `s4`가 case의 `recommended_ingredients`를 효능 근거 없이 후보로 넣어, 근거 없는 성분(알로에신·댕댕이나무열매즙)이 추천되고 `s7` 근거 패널엔 누락됐다.
3. **표시·정렬** — 성분명 원본 노출(`알로에신`·`ALOESIN` 중복), 제품 쏠림(단일 성분·단일 카테고리).

## 1. 확정 결정 (2026-07-23)

| 항목 | 결정 | 사유 |
|---|---|---|
| BSTI 매핑 | **권장성분 가점 강화** | `rec_efficacy.recommended_skin_types`가 63% 빈값·자유텍스트라 하드필터 부적합. skin_type 텍스트 매칭 포기 |
| 제품 개선 | **성분 커버리지 그리디** | 추천 성분을 골고루 커버하는 제품 우선(브랜드 아님). 제품→고민 의미매칭은 데이터 한계로 v1 보류 |
| groundedness | **efficacy 근거 필수** | 최종 추천 성분은 rec_efficacy(효능 사전) 근거 있는 것만. 케이스 언급만으론 부적격 |

## 2. 검색·컨텍스트 레이어

### 2-1. 고민별 커버리지 보장 (문제 1) — `s3_retrieval` · `s4_candidates`

- **원인**: 고민별 검색은 하나 최종 병합·정렬이 score순이라 강한 고민(미백)이 약한 고민(붉어짐)을 밀어냄. 질의 편향이 악화.
- **해결**:
  - (a) `_retrieve_one`의 cases leg 질의를 **고민 중심**으로 재배치 — 고민을 앞·강조하고 사람묘사(BSTI 서술)는 축소. efficacy leg는 이미 고민 구절이라 무변경.
  - (b) `s4` 병합 후 최종 선택에서 **고민당 최소 슬롯 보장** — 각 고민 top-K를 먼저 확보한 뒤 남은 슬롯을 점수순으로 채운다(`candidate.concerns` 태그 활용, 이미 존재).

### 2-2. BSTI 가점 강화 (문제 6) — `s4_candidates` · `constants`

- skin_type 텍스트 매칭은 도입하지 않는다(데이터 부실).
- `bsti_ingredients.recommended_for(bsti_type)` 권장 성분 가점 강화: `BSTI_BOOST` 상향, 건성(D)·민감(S) 축 권장 성분이 `bsti_ingredients`에 실제 있는지 커버리지 점검. 질의 반영은 2-1과 함께 조정.

### 2-3. 성분명 정규화 (문제 5) — `s7_response._case`

- **원인**: `_case`가 `recommended_ingredients`를 원본 그대로 표시(정규화·dedup 없음).
- **해결**: `normalize_ingredient_name` 적용 + 중복 제거(`알로에신`/`ALOESIN` → 1개). 유틸은 이미 존재(`names.py`).

## 3. 생성·근거 레이어

### 3-1. efficacy 근거 필수 (문제 4) — `s4_candidates` · `s6` 프롬프트

- **원인**: `s4 aggregate_candidates`가 efficacy_chunks 성분 + case_chunks의 `recommended_ingredients`를 모두 후보화. case-only 성분(효능 근거 없음)이 추천되고 `s7 _safe_ingredients`(efficacy_chunks만 순회)엔 안 실려 근거 누락.
- **해결**: 최종 추천 후보를 **efficacy 근거 있는 성분으로 제한**한다. case-only 성분은 후보에서 제외(또는 강한 하위로 밀어 실질 미추천). `s6` 프롬프트에 "효능 근거(efficacy doc_id 인용) 있는 성분만 추천"을 명시하고, 생성 후 사후검사로 근거 없는 성분을 제거(기존 환각 차단 흐름 확장).
- **부수 효과**: 데모의 알로에신·댕댕이나무열매즙 문제가 함께 해소된다(추천되면 근거 패널에도 나오거나, 근거 없으면 추천 안 됨).

### 3-2. 고민별 부분 advisory (문제 7) — `s7_response`

- **원인**: advisory가 전체 기준(`case_chunks` 0건일 때만 weak_evidence). 일부 고민만 근거 0건인 경우를 못 잡음.
- **해결**: 고민별 근거 집계 → 근거 0건 고민이 있으면 **부분 경고**(`advisory.code = partial_evidence`, message에 누락 고민 명시). 전체 근거 있음·일부 누락·전무를 구분.

## 4. 제품 레이어

### 4-1. 성분 커버리지 그리디 (문제 2·3) — `s8_products`

- **기준은 브랜드/카테고리가 아니라 추천 성분 커버리지다** (2026-07-23 재확정). 제품이 추천 성분을 많이 담을수록 우선(a·b·c·d 다 담은 제품 > a·b·c > …), 그리고 여러 제품으로 추천 성분을 골고루 커버한다.
- `_select_by_coverage` 그리디: 아직 안 커버된 추천 성분을 가장 많이 더하는 제품부터 고른다(동점이면 전체 매칭 수 → 배합 상위 order_no). 새로 커버할 성분이 없으면 멈춘다 — 이미 나온 성분만 담은 중복 제품은 노출하지 않는다(결과가 `MAX_RECOMMENDED_PRODUCTS` 미만이 될 수 있다).
- 이 방식이 데모의 "단일 성분(쑥잎추출물·헥사펩타이드) 5개 쏠림"을 해소한다. 제품-고민 의미매칭(제품 카테고리↔고민)은 제품 데이터 한계로 v1 보류.

## 5. 변경 범위

| 파일 | 변경 |
|---|---|
| `pipeline/s3_retrieval.py` | cases leg 질의 고민 중심화 (2-1a) |
| `pipeline/s4_candidates.py` | 고민별 최소 슬롯 (2-1b) · efficacy 근거 제한 (3-1) · BSTI 가점 강화 (2-2) |
| `pipeline/s6_generation.py` · `prompts.py` | efficacy 근거 성분만 추천 프롬프트·사후검사 (3-1) |
| `pipeline/s7_response.py` | 성분명 정규화·dedup (2-3) · 고민별 부분 advisory (3-2) |
| `pipeline/s8_products.py` | 성분 커버리지 그리디 선택 (4-1) |
| `constants.py` | `BSTI_BOOST` 조정, advisory 코드 추가 |

## 6. 미결정·리스크

- **고민당 슬롯 K·BSTI_BOOST 값**: 잠정치로 두고 실데이터 QA로 튜닝(03 §5의 임계값 튜닝과 함께).
- **efficacy 근거 제한의 후보 축소**: case-only 성분을 빼면 후보 풀이 좁아질 수 있다 — 민감성 등 희소 고민에서 후보 0개가 늘면 `no_candidates` 응답 빈도↑. QA로 확인, 필요 시 "efficacy 우선, 부족하면 case 허용+표시 구분"으로 완화(현재는 엄격 채택).
- **제품 개수 미달**: 커버 완료 시 멈추므로, 추천 성분이 소수거나 제품에 흩어져 있으면 결과가 `MAX_RECOMMENDED_PRODUCTS` 미만이 될 수 있다 — 중복 성분 제품으로 채우기보다 적더라도 다양한 커버가 낫다는 결정(사용자 확정 2026-07-23).
- **BSTI 커버리지**: `bsti_ingredients`에 건성·민감 축 권장 성분이 빈약하면 가점 강화 효과가 제한적 — 점검 결과에 따라 데이터 보강(지우/호영)이 후속으로 필요할 수 있다.

## 7. BSTI 추천 가지 (⑨) — 2026-07-23 추가

§2-2의 "가점"과 별개로, **BSTI 타입 권장 성분·제품을 응답에 따로 싣는다**. 고민은 "지금 겪는 문제", BSTI는 "타입상 늘 맞는 성분"이라 근거가 다르므로 축을 분리한다(`bsti_ingredients`·`bsti_products`). 두 축에 같은 성분이 겹쳐도 숨기지 않는다 — 근거가 다르기 때문이다.

| 단계 | 구현 | 비고 |
|---|---|---|
| 권장 성분 | `pipeline/s4b_bsti.py` (신규) | `BSTI_RECOMMENDED` 확정 표 → `rec_efficacy` 이름 조회로 효능·주의 근거 부착. 표 순서 유지, `similarity=1.0`(검색이 아니라 확정 매칭) |
| 안전 필터 | `s5_safety` 재사용 | 임신 금기·사용제한을 고민 추천과 똑같이 적용 |
| 제품 | `s8_products` 재사용 | `fetch(candidates, names, owned)` 로 일반화(LLM 결합 제거) — 커버리지 그리디 동일 |
| 배선 | `service._bsti_branch` | ⑥ 생성과 `asyncio.gather` 병렬(DB 왕복을 LLM 대기에 흡수). 어떤 실패도 빈 결과로 격리해 핵심 추천을 지킨다 |

**결정: LLM 미관여.** BSTI 권장은 확정 표라 생성이 개입할 이유가 없고, 근거(효능 사전)·경고·제품은 모두 코드가 붙이는 사실이다.

**실 DB 점검 (2026-07-23)**: DSPW 권장 성분은 별칭 확장 후 19개가 **전부** `rec_efficacy`에 매칭됐다. 다만 `세라마이드`·`아젤라익애씨드`·`펩타이드`는 `ingredient_id`가 null이라(식약처 미매핑) 제품 매칭에서 빠진다 — 성분 카드에는 정상 노출된다.

**한계**: `rec_efficacy.name_kr` 정확 일치만 싣는다. 원본에 개행·괄호가 섞인 이름(2,270행 중 27건)은 매칭되지 않아 빠질 수 있다 — DSPW에선 미발현이나 다른 타입은 QA가 필요하다.

## 8. 종합 추천 (⑩) — 2026-07-23 추가

§7의 BSTI 축과 고민 축은 근거가 달라 따로 싣지만, 사용자가 결국 보는 건 "그래서 뭘 쓰면 되나"다. 두 축을 합친 대표를 `top_ingredients`·`top_products`(각 최대 5개)로 낸다 — 프론트 **메인 카드**이고, 나머지 목록은 "왜 뽑혔나"의 상세 근거다.

**선정 규칙** (`pipeline/s9_top.select_top`, LLM 미관여 — 두 축 산출을 순위로 합칠 뿐):

| 순위 | 대상 | `match_source` |
|---|---|---|
| 1 | 고민 추천 **∩** BSTI 권장 | `both` |
| 2 | 고민 추천 (score 순) | `concern` |
| 3 | BSTI 권장 (표 순서) | `bsti` |

- 고민 축은 `narrative.recommended_names`(⑥이 사용자에게 **실제로 보인** 성분)로 한정한다 — 후보에만 있고 서사에 없는 성분을 대표로 올리면 설명 없는 추천이 된다.
- 제품은 이 대표 성분으로 ⑧ 커버리지 그리디를 재사용한다(상한 `MAX_RECOMMENDED_PRODUCTS`). 고민 제품 조회와 병렬로 돈다.
- `IngredientEvidence.match_source`로 출처를 실어 프론트가 "고민·타입 모두 적합" 배지를 달 수 있게 한다. 다른 목록에서는 `null`이다(축이 이미 정해져 있으므로).

**결정: 서사는 고민 축만 설명한다.** 종합에 BSTI-only 성분이 들어가면 `answer`에 그 설명이 없지만, 카드가 `efficacy`(효능 사전 문장)를 담고 있어 설명이 비지 않는다. LLM에 BSTI 후보까지 넘기는 방식은 프롬프트·비용이 늘고 고민과 무관한 성분을 추천할 위험이 있어 채택하지 않았다.
