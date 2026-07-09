from supabase import AsyncClient, acreate_client

from app.core.config import get_settings

# async 라우터에서 이벤트 루프를 막지 않도록 동기 Client 대신 AsyncClient를 쓴다.
# acreate_client가 코루틴이라 lru_cache로 감쌀 수 없어 모듈 전역 지연 싱글턴으로 둔다.
_client: AsyncClient | None = None


async def get_supabase() -> AsyncClient:
    """서버용 Supabase 비동기 클라이언트 (service role).

    service role 키는 RLS를 우회하므로, user 소유 데이터를 다루는 쿼리는
    반드시 user_id로 직접 필터링해야 한다 (docs/conventions.md 참고).
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = await acreate_client(settings.supabase_url, settings.supabase_service_role_key)
    return _client
