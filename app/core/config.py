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

    # 웹(Flutter web) 클라이언트의 cross-origin 요청 허용 목록.
    # 네이티브 앱은 CORS와 무관하지만, 웹 빌드는 브라우저가 이 헤더를 요구한다.
    # 배포 웹 origin(Vercel 등)은 .env 의 CORS_ORIGINS 로 덧붙인다.
    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
