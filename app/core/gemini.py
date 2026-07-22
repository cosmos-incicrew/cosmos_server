from functools import lru_cache

from google import genai
from google.oauth2 import service_account

from app.core.config import get_settings


@lru_cache
def get_gemini() -> genai.Client:
    """GCP_PROJECT_ID 가 있으면 Vertex, 없으면 AI Studio.

    Vertex 는 API 키 대신 ADC 를 쓴다 — GOOGLE_APPLICATION_CREDENTIALS 가
    서비스 계정 키 파일을 가리켜야 한다. 모델 ID·호출부는 양쪽이 동일하다.
    """
    settings = get_settings()
    if settings.gcp_project_id:
        credentials = None
        if settings.google_application_credentials:
            # google-auth 쪽이 타입 미표기 — strict 모드에서만 걸린다.
            credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        return genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            credentials=credentials,
        )
    return genai.Client(api_key=settings.gemini_api_key)


def gemini_model_for(complex_query: bool = False) -> str:
    """Flash 기본, 복합 질의만 Pro (아키텍처 설계서 7장 성능 방안)."""
    settings = get_settings()
    return settings.gemini_model_pro if complex_query else settings.gemini_model_flash
