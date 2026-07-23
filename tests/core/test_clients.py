from app.core.config import get_settings
from app.core.gemini import gemini_model_for, get_gemini
from app.core.langfuse import get_langfuse
from app.core.supabase import get_supabase


async def test_supabase_client_is_singleton():
    assert await get_supabase() is await get_supabase()


def test_gemini_client_is_singleton():
    assert get_gemini() is get_gemini()


def test_langfuse_client_is_singleton():
    assert get_langfuse() is get_langfuse()


def test_gemini_model_is_single_source():
    """모델 설정은 `gemini_model` 하나로 통합됐다 (구 flash/pro 분기는 폐기)."""
    assert gemini_model_for() == get_settings().gemini_model
