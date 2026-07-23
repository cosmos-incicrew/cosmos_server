from functools import lru_cache

from google import genai
from google.oauth2 import service_account

from app.core.config import get_settings


@lru_cache
def get_gemini() -> genai.Client:
    """Vertex(Agent Platform) 전용. API 키가 아니라 ADC 를 쓴다 —
    GOOGLE_APPLICATION_CREDENTIALS 가 서비스 계정 키 파일을 가리켜야 한다.
    """
    settings = get_settings()
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


def gemini_model_for() -> str:
    return get_settings().gemini_model
