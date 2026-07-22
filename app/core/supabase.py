from typing import Any

from supabase import AsyncClient, acreate_client

from app.core.config import get_settings

# async 라우터에서 이벤트 루프를 막지 않도록 동기 Client 대신 AsyncClient를 쓴다.
# acreate_client가 코루틴이라 lru_cache로 감쌀 수 없어 모듈 전역 지연 싱글턴으로 둔다.
_client: AsyncClient | None = None


async def create_supabase_client(supabase_url: str, supabase_service_role_key: str) -> AsyncClient:
    """명시한 설정으로 캐시되지 않는 Supabase 비동기 클라이언트를 만든다."""
    return await acreate_client(supabase_url, supabase_service_role_key)


async def get_supabase() -> AsyncClient:
    """서버용 Supabase 비동기 클라이언트 (service role).

    service role 키는 RLS를 우회하므로, user 소유 데이터를 다루는 쿼리는
    반드시 user_id로 직접 필터링해야 한다 (docs/conventions.md 참고).

    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = await create_supabase_client(
            settings.supabase_url, settings.supabase_service_role_key
        )
    return _client


def rows(result: Any) -> list[dict[str, Any]]:
    """Supabase 응답의 느슨한 JSON 타입을 행 dict 목록으로 좁힌다.

    postgrest 의 `.data` 는 `list[dict] | list[str] | ...` 로 타입이 넓어 그대로 쓰면
    `row["col"]` 마다 mypy 가 막는다.

    현재 소비자는 recommendations 하나뿐이다. core 에 둔 이유는 `.data` 의 넓은 타입이
    postgrest 를 쓰는 모든 모듈에 똑같이 나타나기 때문이며, 실제로 product_compare 와
    ingredient_search 에 같은 내용의 사설 `_rows` 가 각각 있다. 그 둘의 교체는 소유자
    (박영기)의 모듈이라 별도 PR 로 미룬다 — 이 함수는 그때 실사용자가 3곳이 된다.
    """
    return [row for row in (result.data or []) if isinstance(row, dict)]
