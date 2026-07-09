from functools import lru_cache

from google import genai

from app.core.config import get_settings


@lru_cache
def get_gemini() -> genai.Client:
    return genai.Client(api_key=get_settings().gemini_api_key)


def gemini_model_for(complex_query: bool = False) -> str:
    """Flash 기본, 복합 질의만 Pro (아키텍처 설계서 7장 성능 방안)."""
    settings = get_settings()
    return settings.gemini_model_pro if complex_query else settings.gemini_model_flash
