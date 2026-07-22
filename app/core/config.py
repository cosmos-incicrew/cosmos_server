from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """서버 전역 설정. .env 또는 환경 변수에서 로딩하며 필수값 누락 시 기동에 실패한다."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    supabase_url: str
    supabase_service_role_key: str
    # 레거시 HS256 시크릿. 안 쓴다 (토큰은 ES256, app/core/auth.py 에서 JWKS 검증).
    # 필드를 지우면 extra=forbid 때문에 이 줄이 남은 기존 .env 가 기동에 실패한다.
    supabase_jwt_secret: str = ""
    # 회원 탈퇴 시 카카오 앱 연결을 끊는 데 쓴다 (app/core/kakao.py).
    # Kakao Developers → 앱 설정 → 앱 키 → Admin 키.
    # 비어 있으면 연결 해제를 건너뛴다 — 계정 삭제 자체는 그대로 동작한다.
    kakao_admin_key: str = ""
    # AI Studio 모드에서만 쓴다. Vertex 모드(GCP_PROJECT_ID 지정)면 비워둔다.
    gemini_api_key: str = ""
    # 이 값이 있으면 Vertex(Agent Platform), 없으면 AI Studio. app/core/gemini.py 참고.
    # 인증은 API 키가 아니라 ADC — GOOGLE_APPLICATION_CREDENTIALS 로 키 파일을 가리킨다.
    gcp_project_id: str = ""
    # Gemini 3.x 는 global 엔드포인트에서만 서비스된다 — us-central1·asia-northeast3 는 404.
    gcp_location: str = "global"
    # 서비스 계정 키 파일 경로. 필드로 받는 이유: pydantic 은 .env 값을 os.environ 으로
    # 내보내지 않아, 이름만 같게 적어두면 google-auth 가 못 읽고 만료된 ADC 로 폴백한다.
    # 비우면 ADC 기본 탐색(gcloud 로그인, Render 의 실제 환경변수)에 맡긴다.
    google_application_credentials: str = ""
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str = "https://cloud.langfuse.com"
    gemini_model_flash: str = "gemini-3.6-flash"
    # Pro 는 2.5 유지 — 3.x Pro 는 아직 preview 뿐이라 종료 예고가 짧다 (발표 7/27).
    gemini_model_pro: str = "gemini-2.5-pro"
    product_compare_max_count: int = 4
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
