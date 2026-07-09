from functools import lru_cache

from langfuse import Langfuse

from app.core.config import get_settings


@lru_cache
def get_langfuse() -> Langfuse:
    settings = get_settings()
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
