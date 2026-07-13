from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서버 전역 설정. .env 또는 환경 변수에서 로딩하며 필수값 누락 시 기동에 실패한다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    supabase_url: str
    supabase_service_role_key: str
    supabase_jwt_secret: str
    gemini_api_key: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str = "https://cloud.langfuse.com"
    gemini_model_flash: str = "gemini-2.5-flash"
    gemini_model_pro: str = "gemini-2.5-pro"
    product_compare_max_count: int = 4
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
