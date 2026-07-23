from functools import lru_cache

from google import genai

from app.core.config import get_settings


@lru_cache
def get_gemini() -> genai.Client:
    """GCP_PROJECT_ID 가 있으면 Vertex, 없으면 AI Studio.

    Vertex 는 API 키나 JSON 키 파일 대신 런타임 ADC를 쓴다.
    모델 ID·호출부는 양쪽이 동일하다.
    """
    settings = get_settings()
    if settings.gcp_project_id:
        return genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
    return genai.Client(api_key=settings.gemini_api_key)


def gemini_model_for(complex_query: bool = False) -> str:
    """Flash 기본, 복합 질의만 Pro (아키텍처 설계서 7장 성능 방안)."""
    settings = get_settings()
    return settings.gemini_model_pro if complex_query else settings.gemini_model_flash
