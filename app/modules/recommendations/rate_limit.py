"""추천 엔드포인트 rate limit — 사용자당 호출 빈도 상한.

1회 호출이 검색 6쿼리 + Gemini 생성(재시도 포함 최대 2회 LLM)이라, 인증 사용자가
루프로 두드리면 과금 폭증 + 워커 고갈(무료 티어 DoS)로 번진다. 타임아웃은 워커 무한
대기만 막지 호출 빈도는 안 막으므로 여기서 막는다.

ponytail: 인프로세스 슬라이딩 윈도우다. Render 다중 워커에선 워커마다 카운트가 독립이라
실질 전역 한도는 (워커 수 × RATE_LIMIT_MAX)가 된다. 정확한 전역 한도가 필요해지면
Redis 등 공유 저장소로 옮긴다 — 현재 목적(단일 사용자 루프 남용 차단)에는 인프로세스로 충분.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Depends

from app.core.auth import verify_jwt
from app.modules.recommendations import errors
from app.modules.recommendations.constants import RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_SECONDS

# user_id → 최근 호출 시각(monotonic) 큐. 윈도우 밖 항목은 조회 시 앞에서 버린다.
_hits: dict[str, deque[float]] = defaultdict(deque)
# rate_limited_user 는 동기 def 의존성이라 FastAPI 가 스레드풀에서 실행한다 — 같은
# user_id 요청이 여러 스레드로 동시에 들어와 _hits 를 check-then-act 로 건드리면 상한을
# 초과 통과한다. 이벤트 루프 단일 스레드가 아니므로 asyncio.Lock 이 아니라 threading.Lock.
_lock = threading.Lock()


def enforce_rate_limit(user_id: str) -> None:
    """윈도우 내 호출이 상한을 넘으면 429. 넘지 않으면 이번 호출을 기록한다.

    빈 deque 는 한 번 호출하고 안 돌아온 사용자만큼 남는다 — 프로세스 수명 내 고유
    사용자 수에 비례하며 재시작 시 해소된다. 정확한 상한이 필요하면 주기적 스윕을 건다.
    """
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    with _lock:
        hits = _hits[user_id]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_MAX:
            raise errors.too_many_requests()
        hits.append(now)


def rate_limited_user(user_id: Annotated[str, Depends(verify_jwt)]) -> str:
    """verify_jwt 로 사용자를 확인한 뒤 rate limit 을 적용하는 라우터 의존성."""
    enforce_rate_limit(user_id)
    return user_id


def _reset() -> None:
    """테스트 전용 — 윈도우 상태를 비운다."""
    _hits.clear()
