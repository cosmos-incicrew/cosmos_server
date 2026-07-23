from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서버 전역 설정. .env 또는 환경 변수에서 로딩하며 필수값 누락 시 기동에 실패한다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    supabase_url: str
    supabase_service_role_key: str
    supabase_jwt_secret: str = ""

    kakao_admin_key: str = ""

    gcp_project_id: str = ""
    # Gemini 3.x 는 global 엔드포인트에서만 서비스된다 — us-central1·asia-northeast3 는 404.
    gcp_location: str = "global"
    google_application_credentials: str = ""

    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str = "https://cloud.langfuse.com"

    gemini_model: str = "gemini-3.5-flash-lite"
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 1536
    
    product_compare_max_count: int = 4
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
