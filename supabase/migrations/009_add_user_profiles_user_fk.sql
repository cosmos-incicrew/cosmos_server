-- user_profiles.user_id → auth.users(id) 외래키 (회원·마이페이지 모듈, 소유: 김민경)
-- 007 에서 user_id 를 PK 로만 두어 auth.users 와 연결이 없었다.
-- 지금은 서버가 JWT 검증으로 얻은 user_id 만 쓰므로 잘못된 값이 들어갈 일은 없지만,
-- 계정이 삭제되면 프로필 행이 고아로 남는다 — 탈퇴 기능이 들어가기 전에 걸어둔다.

-- 고아 행이 있으면 FK 추가가 실패한다. 그렇다고 여기서 지우지는 않는다.
--
-- 이 FK 가 걸린 뒤로 고아는 생길 수 없다(on delete cascade). 그러니 앞으로 고아가
-- 발견된다면 정리할 쓰레기가 아니라 조사할 이상 징후다 — 예컨대 auth 스키마만 다른
-- 시점 백업으로 복원한 경우. 그 상태에서 자동으로 지우면 멀쩡한 프로필이 되돌릴 수
-- 없게 사라진다. 되돌릴 수 없는 일은 사람 판단 뒤로 미룬다.
--
-- 신규 환경에서는 테이블이 비어 있어 이 블록은 아무 일도 하지 않는다.
do $$
declare
    orphan_count bigint;
begin
    select count(*) into orphan_count
    from public.user_profiles p
    where not exists (select 1 from auth.users u where u.id = p.user_id);

    if orphan_count > 0 then
        raise exception
            '고아 프로필 %건 — auth.users 에 없는 user_id 입니다. 확인 후 직접 처리하세요: '
            'select * from public.user_profiles p where not exists '
            '(select 1 from auth.users u where u.id = p.user_id);',
            orphan_count;
    end if;
end $$;

alter table public.user_profiles
    drop constraint if exists user_profiles_user_id_fkey;

alter table public.user_profiles
    add constraint user_profiles_user_id_fkey
    foreign key (user_id) references auth.users(id) on delete cascade;
