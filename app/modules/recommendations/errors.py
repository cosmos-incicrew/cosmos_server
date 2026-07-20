"""추천 엔드포인트가 내는 에러 — 코드·문구를 한곳에 모은다.

에러 코드는 docs/design/01-recommendations-pipeline.md §3 계약이다. 여러 단계가
같은 코드를 던지므로(①·③이 모두 DB_UNAVAILABLE) 문구가 갈라지지 않게 여기서만 만든다.
"""

from fastapi import HTTPException, status


def onboarding_required() -> HTTPException:
    """409 — 프로필에 나이 또는 피부 고민이 없다. 프론트가 온보딩 화면으로 보낸다."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "PROFILE_ONBOARDING_REQUIRED",
            "message": "추천을 받으려면 먼저 나이와 피부 고민을 입력해 주세요.",
        },
    )


def db_unavailable() -> HTTPException:
    """503 — Supabase 조회 실패."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "DB_UNAVAILABLE", "message": "일시적으로 조회할 수 없습니다."},
    )


def llm_upstream_error() -> HTTPException:
    """502 — Gemini 호출이 재시도 후에도 실패."""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "LLM_UPSTREAM_ERROR", "message": "추천 생성에 실패했습니다."},
    )
