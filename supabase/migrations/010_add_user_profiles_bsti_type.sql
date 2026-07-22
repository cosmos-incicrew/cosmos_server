-- BSTI 16타입 현재 값 (회원·마이페이지 모듈, 소유: 김민경)
-- 추천 ① 컨텍스트 조립이 "가장 최근 진단 1건"만 쓰므로(01 §2-①) 이력이 아니라
-- 현재 타입만 둔다. 재검사하면 덮어쓴다.
--
-- BSTI 지식 테이블 3종(bsti_user_diagnoses / bsti_type_ingredients / bsti_ingredients)은
-- 만들지 않고 타입→성분 매핑을 클라이언트 상수로 처리하기로 정해졌다(2026-07-21).
-- 그래서 서버가 갖는 BSTI 정보는 이 컬럼 하나뿐이다 — "이 사용자의 타입이 무엇인가".

alter table public.user_profiles
    add column if not exists bsti_type text;

-- 16타입은 4축 조합이다: O/D(유수분) S/R(민감) P/N(색소) W/T(주름).
-- 오타나 소문자가 들어오면 매핑 조인이 조용히 0건이 되어 추천에서 BSTI가 사라진다.
alter table public.user_profiles
    drop constraint if exists user_profiles_bsti_type_check;

alter table public.user_profiles
    add constraint user_profiles_bsti_type_check
    check (bsti_type is null or bsti_type ~ '^[OD][SR][PN][WT]$');

comment on column public.user_profiles.bsti_type is
    'BSTI 16타입 코드 (예: OSPW). NULL = 검사 전. 재검사 시 덮어쓴다(이력 미보존).';
