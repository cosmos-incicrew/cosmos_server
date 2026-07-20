-- 성분 추천(recommendations) 도메인 테이블: rec_cases · rec_efficacy
-- 설계 진실 공급원: docs/design/02-recommendations-data-spec.md §2
-- 소유: 김민경(테이블·제약·적재). embedding 값 채우기와 HNSW/GIN 인덱스는
--       서지우(공유 벡터 인프라, 02 §4) — 이 마이그레이션은 인덱스를 만들지 않는다.

create extension if not exists vector with schema extensions;

-- ── 상담 사례 8,000건 ─────────────────────────────────────────────
-- 한 상담이 한 행. question 에만 임베딩을 붙인다(사용자 질의는 "상황 서술"이라
-- 비교 대상도 상황 서술이어야 하기 때문 — 02 §2 임베딩 대상 원칙).
create table if not exists public.rec_cases (
    case_id                 text primary key,
    target_concern          text not null,
    skin_concerns           text[] not null default '{}',
    question                text not null,
    answer                  text not null,
    cot                     jsonb not null default '[]'::jsonb,
    recommended_ingredients text[] not null default '{}',
    evidence_sources        text[] not null default '{}',
    gender                  text,
    age                     smallint,
    skin_type               text,
    initial_skin_condition  text,
    embedding               extensions.vector(1536),
    constraint rec_cases_target_concern_check check (target_concern in (
        '과각질/악건성', '모공', '미백(색소침착/기미/칙칙함)', '민감성(트러블/자극감)',
        '붉어짐(홍조)', '여드름/뾰루지', '주름', '피부처짐/탄력저하'
    ))
);

comment on table public.rec_cases is
    'AI Hub 71886 상담 사례. 추천 RAG의 "유사 상담" 검색용. 임베딩·인덱스=서지우.';
comment on column public.rec_cases.question is
    '임베딩 대상 — 사용자 질의와 상황↔상황 매칭';
comment on column public.rec_cases.skin_concerns is
    '복수 고민(배열). 검색 시 배열 겹침 매칭 — 희소 고민 보완용(01 §2-③)';
comment on column public.rec_cases.embedding is
    'vector(1536) · 서지우 생성. NULL = 미임베딩';

-- ── 성분 지식 2,465종 ─────────────────────────────────────────────
-- 성분 하나가 한 행. name_kr + efficacy + product_traits 에만 임베딩.
-- PK가 자동 생성 id라, 재실행 멱등을 위해 (inci, name_kr) UNIQUE 를 conflict
-- target 으로 둔다 (없으면 재실행마다 2,465행이 통째로 중복 적재 — 02 §2).
create table if not exists public.rec_efficacy (
    id                        bigint generated always as identity primary key,
    inci                      text,
    name_kr                   text,
    efficacy                  text not null,
    product_traits            text,
    recommended_skin_types    text,
    safety_note               text,
    recommended_concentration text,
    regulation_note           text,
    reference_source          text,
    -- 화학적 물성은 의미 검색에 노이즈라 임베딩엔 제외하되 페이로드로 보존(02 §2)
    properties                text,
    solubility                text,
    formula                   text,
    weight                    text,
    source                    text,
    ingredient_id             bigint references public.ingredients (ingredient_id),
    embedding                 extensions.vector(1536),
    constraint rec_efficacy_name_present check (inci is not null or name_kr is not null),
    constraint rec_efficacy_inci_name_uniq unique nulls not distinct (inci, name_kr)
);

comment on table public.rec_efficacy is
    'AI Hub 71886 성분-효능 지식. 추천 RAG의 "고민→효능 성분" 검색용. 식약처 ingredients와 ingredient_id로 연결(테이블 병합 아님 — 02 §3).';
comment on column public.rec_efficacy.embedding is
    'vector(1536) · 서지우 생성. NULL = 미임베딩';

-- 서버(service_role)만 접근. RLS 활성 + 정책 미부여로 anon/authenticated 직접
-- 접근을 차단한다 (기존 ingredients·restrictions와 동일 정책).
alter table public.rec_cases enable row level security;
alter table public.rec_efficacy enable row level security;
