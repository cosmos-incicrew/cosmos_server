"""테스트 공통 픽스처. import 시점에 테스트용 환경 변수를 주입한다."""

import os

import pytest

from app.core.config import get_settings

TEST_ENV = {
    "SUPABASE_URL": "http://localhost:54321",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    # 가짜 값이지만 **비우면 안 된다.** google-genai 는 project 가 비면 그때
    # `google.auth.default()` 로 ADC 를 찾아 나서고(_api_client.py `load_auth`),
    # 로컬에 ADC 가 없으면 클라이언트 생성 자체가 DefaultCredentialsError 로 죽는다.
    # project 가 차 있으면 인증은 첫 요청까지 미뤄져 생성만으로는 밖으로 안 나간다.
    "GCP_PROJECT_ID": "test-project",
    # ADC 는 이 변수도 자격 증명 출처로 읽는다 — .env 의 실제 키 파일을 테스트가 집어
    # 들지 않게 비운다 (conventions.md §테스트).
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "LANGFUSE_PUBLIC_KEY": "test-langfuse-public",
    "LANGFUSE_SECRET_KEY": "test-langfuse-secret",
    "LANGFUSE_BASE_URL": "http://langfuse.test",
    # 가짜 키로도 SDK 백그라운드 익스포터는 클라우드에 붙어 401 을 반복한다.
    # 키를 지우는 것으론 못 막는다(호스트 기본값이 클라우드다) — SDK 스위치로 끈다.
    "LANGFUSE_TRACING_ENABLED": "false",
    "CORS_ALLOWED_ORIGINS": '["https://cosmos-incicrew.vercel.app"]',
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
