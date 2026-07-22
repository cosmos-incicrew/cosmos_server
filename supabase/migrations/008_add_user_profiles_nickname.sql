-- 온보딩 프로필에 화면 표시용 별명 추가 (명세서 A-2)
-- 007 작성 시점에는 추천 컨텍스트에 필요한 컬럼만 넣어 nickname 이 빠져 있었다.

alter table public.user_profiles
    add column if not exists nickname text;

comment on column public.user_profiles.nickname is '마이페이지·홈에서 표시할 별명';
