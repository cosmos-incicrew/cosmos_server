import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from tests.conftest import TEST_ENV


def test_settings_load_from_env():
    settings = Settings()
    assert settings.supabase_url == TEST_ENV["SUPABASE_URL"]
    assert settings.gemini_model_flash  # 기본값 존재


def test_settings_fail_without_required_env(monkeypatch, tmp_path):
    """필수값 누락 시 기동 시점에 즉시 실패해야 한다 (스펙 3장)."""
    for key in TEST_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # 실제 .env 파일의 영향 차단
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_is_singleton():
    assert get_settings() is get_settings()
