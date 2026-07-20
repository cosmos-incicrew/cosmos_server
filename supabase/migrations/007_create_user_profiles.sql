-- 온보딩 프로필 (회원·마이페이지 모듈, 소유: 김민경)
-- 성분 추천 ① 컨텍스트 조립의 필수 입력 — docs/design/01-recommendations-pipeline.md §2-①
-- 나이 또는 피부 고민이 없으면 추천은 409 PROFILE_ONBOARDING_REQUIRED 로 막는다.

create table if not exists public.user_profiles (
    user_id       uuid primary key,
    age           smallint,
    gender        text,
    skin_concerns text[] not null default '{}',
    -- 임신·수유 금기 검사(01 §2-⑤)용. NULL = 온보딩 미수집(unknown) → 제거 아닌 경고.
    is_pregnant   boolean,
    is_nursing    boolean,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint user_profiles_gender_check check (gender in ('female', 'male', 'other'))
);

comment on table public.user_profiles is
    '온보딩 프로필. skin_concerns 는 app/common/skin_concerns.py 의 영문 코드 8종.';
comment on column public.user_profiles.is_pregnant is
    'NULL = 미수집(unknown) — 금기 성분을 제거하지 않고 경고만 부착 (01 §2-⑤)';

-- 서버(service_role)만 접근. RLS 활성 + 정책 미부여 (기존 테이블과 동일 정책).
alter table public.user_profiles enable row level security;
