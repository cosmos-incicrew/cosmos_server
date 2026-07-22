"""카카오 연결 해제 (회원 탈퇴 시).

Supabase 계정을 지워도 카카오 앱 연결은 남는다 — 사용자의 카카오계정에 서비스가
그대로 보이고 재로그인 시 동의 화면도 안 뜬다.

Admin 키로 호출하는 이유: 탈퇴 시점에는 사용자의 카카오 토큰이 만료됐거나
세션 갱신 과정에서 사라졌을 수 있다.
"""

import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)

_UNLINK_URL = "https://kapi.kakao.com/v1/user/unlink"
_TIMEOUT_SECONDS = 5.0


async def unlink(settings: Settings, kakao_user_id: str) -> bool:
    """카카오 앱 연결을 끊는다. 성공하면 True.

    실패해도 예외를 올리지 않는다 — 계정 삭제가 이미 끝난 뒤라 여기서 터뜨리면
    "탈퇴는 됐는데 500 을 받는" 혼란만 남는다.
    """
    if not settings.kakao_admin_key:
        logger.info("KAKAO_ADMIN_KEY 미설정 — 카카오 연결 해제를 건너뛴다")
        return False

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _UNLINK_URL,
                headers={"Authorization": f"KakaoAK {settings.kakao_admin_key}"},
                data={"target_id_type": "user_id", "target_id": kakao_user_id},
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # 4xx는 설정 오류다 (Admin 키 오타, REST 키 오입력 등). 일시 장애와 달리
        # 저절로 낫지 않고 모든 탈퇴에서 조용히 실패하므로 등급을 올린다.
        level = logging.ERROR if exc.response.status_code < 500 else logging.WARNING
        logger.log(
            level,
            "카카오 연결 해제 실패 status=%s (계정 삭제는 완료됨) kakao_user_id=%s",
            exc.response.status_code,
            kakao_user_id,
            exc_info=True,
        )
        return False
    except httpx.HTTPError:
        # 네트워크·타임아웃 — 계정은 지워졌고 카카오 연결만 남는다.
        logger.warning(
            "카카오 연결 해제 실패 (네트워크, 계정 삭제는 완료됨) kakao_user_id=%s",
            kakao_user_id,
            exc_info=True,
        )
        return False

    logger.info("카카오 연결 해제 완료 kakao_user_id=%s", kakao_user_id)
    return True
