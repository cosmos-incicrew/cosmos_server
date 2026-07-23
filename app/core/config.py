from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서버 전역 설정. .env 또는 환경 변수에서 로딩하며 필수값 누락 시 기동에 실패한다."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_known_legacy_keys(cls, data: object) -> object:
        """알려진 로컬 키만 이전하고 다른 오타는 실패시킨다."""
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        cleaned.pop("supabase_jwt_secret", None)
        cleaned.pop("SUPABASE_JWT_SECRET", None)
        legacy_langfuse_host = cleaned.pop("langfuse_host", None)
        legacy_langfuse_host = cleaned.pop("LANGFUSE_HOST", legacy_langfuse_host)
        if legacy_langfuse_host is not None:
            cleaned.setdefault("langfuse_base_url", legacy_langfuse_host)
        return cleaned

    supabase_url: str
    supabase_service_role_key: str
    # 회원 탈퇴 시 카카오 앱 연결을 끊는 데 쓴다 (app/core/kakao.py).
    # Kakao Developers → 앱 설정 → 앱 키 → Admin 키.
    # 비어 있으면 연결 해제를 건너뛴다 — 계정 삭제 자체는 그대로 동작한다.
    kakao_admin_key: str = ""
    # AI Studio 모드에서만 쓴다. Vertex 모드(GCP_PROJECT_ID 지정)면 비워둔다.
    gemini_api_key: str = ""
    # 이 값이 있으면 Vertex(Agent Platform), 없으면 AI Studio. app/core/gemini.py 참고.
    # 인증은 API 키나 JSON 키 파일이 아니라 런타임의 ADC를 사용한다.
    gcp_project_id: str = ""
    # Gemini 3.x 는 global 엔드포인트에서만 서비스된다 — us-central1·asia-northeast3 는 404.
    gcp_location: str = "global"
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_tracing_enabled: bool = True
    gemini_model_flash: str = "gemini-3.6-flash"
    # Pro 는 2.5 유지 — 3.x Pro 는 아직 preview 뿐이라 종료 예고가 짧다 (발표 7/27).
    gemini_model_pro: str = "gemini-2.5-pro"
    product_compare_max_count: int = 4
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
