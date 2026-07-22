"""개발용 Supabase 액세스 토큰 발급기 (Swagger 인증 테스트용).

서버는 Supabase 가 ES256 으로 서명한 토큰만 받는다(app/core/auth.py) — 손으로 찍을 수
없다. 이 프로젝트는 OAuth(구글·카카오)만 열려 있어 이메일+비밀번호 로그인이 꺼져 있으므로,
service_role 키로 테스트 유저를 만들고 admin 매직링크 토큰을 세션으로 교환해 "진짜"
토큰을 받는다. (메일은 실제로 발송되지 않는다 — admin 이 링크만 반환한다.)

    uv run python scripts/dev_token.py

출력된 토큰을 Swagger 의 Authorize 창에 넣으면 된다. 토큰은 1시간 정도 유효하므로
만료되면 이 스크립트를 다시 돌리면 된다.

주의: 운영 DB 에 절대 돌리지 말 것. 테스트 유저를 실제로 생성한다.
"""

from supabase import create_client

from app.core.config import get_settings

# 고정 테스트 계정. 팀원 누구나 이 스크립트로 같은 토큰 흐름을 재현할 수 있게 상수로 둔다.
_TEST_EMAIL = "swagger-test@example.com"


def main() -> None:
    settings = get_settings()
    client = create_client(settings.supabase_url, settings.supabase_service_role_key)

    # 유저가 없으면 만든다. 이미 있으면 create_user 가 에러를 내므로 무시한다.
    try:
        client.auth.admin.create_user(
            {"email": _TEST_EMAIL, "email_confirm": True}
        )
        print(f"테스트 유저 생성: {_TEST_EMAIL}")
    except Exception as exc:  # noqa: BLE001 - 이미 존재 등 어떤 실패든 토큰 발급으로 넘어간다
        print(f"유저 생성 건너뜀 ({type(exc).__name__}) — 이미 있으면 정상")

    # admin 매직링크: 메일 발송 없이 일회용 토큰만 반환한다. 이메일 로그인이 꺼져 있어도 동작.
    link = client.auth.admin.generate_link(
        {"type": "magiclink", "email": _TEST_EMAIL}
    )
    # 그 일회용 토큰을 실제 세션(access_token)으로 교환한다.
    result = client.auth.verify_otp(
        {"token_hash": link.properties.hashed_token, "type": "email"}
    )
    if result.session is None:
        raise SystemExit("토큰 교환 실패 — service_role 키/Supabase 설정 확인")

    token = result.session.access_token
    print("\n=== Swagger Authorize 창에 붙여넣을 값 (Bearer 는 빼고 토큰만) ===\n")
    print(token)
    print(f"\nuser_id(sub): {result.user.id if result.user else '?'}")


if __name__ == "__main__":
    main()
