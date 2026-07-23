from functools import lru_cache

from google import genai

from app.core.config import get_settings


@lru_cache
def get_gemini() -> genai.Client:
    """Vertex AI 클라이언트. 로컬 또는 GCE 런타임의 ADC를 사용한다."""
    settings = get_settings()
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project_id,
        location=settings.gcp_location,
    )


def gemini_model_for() -> str:
    return get_settings().gemini_model
