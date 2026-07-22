"""테스트 공통 픽스처. import 시점에 테스트용 환경 변수를 주입한다."""

import os

import pytest

from app.core.config import get_settings

TEST_ENV = {
    "SUPABASE_URL": "http://localhost:54321",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    "GEMINI_API_KEY": "test-gemini-key",
    # 빈 값으로 고정해 테스트가 AI Studio 모드에 머물게 한다.
    # 안 그러면 .env 의 GCP_PROJECT_ID 를 읽어 실제 GCP 인증을 시도한다.
    "GCP_PROJECT_ID": "",
    "LANGFUSE_PUBLIC_KEY": "test-langfuse-public",
    "LANGFUSE_SECRET_KEY": "test-langfuse-secret",
}

for _key, _value in TEST_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings는 lru_cache라 한 테스트의 설정 오버라이드가 다음 테스트로 샌다.
    각 테스트 전후로 캐시를 비워 격리한다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
