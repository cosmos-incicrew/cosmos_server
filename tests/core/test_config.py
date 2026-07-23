import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from tests.conftest import TEST_ENV


def test_settings_load_from_env():
    settings = Settings()
    assert settings.supabase_url == TEST_ENV["SUPABASE_URL"]
    assert settings.langfuse_base_url == TEST_ENV["LANGFUSE_BASE_URL"]
    assert settings.langfuse_tracing_enabled is False
    assert settings.cors_allowed_origins == ["https://cosmos-incicrew.vercel.app"]
    assert not hasattr(settings, "supabase_jwt_secret")
    assert not hasattr(settings, "google_application_credentials")
    assert settings.gemini_model  # 기본값 존재


def test_settings_fail_without_required_env(monkeypatch, tmp_path):
    """필수값 누락 시 기동 시점에 즉시 실패해야 한다 (스펙 3장)."""
    for key in TEST_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # 실제 .env 파일의 영향 차단
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_ignore_only_removed_supabase_jwt_secret(tmp_path):
    legacy_env = tmp_path / ".env"
    legacy_env.write_text("SUPABASE_JWT_SECRET=removed\n", encoding="utf-8")

    settings = Settings(_env_file=legacy_env)

    assert not hasattr(settings, "supabase_jwt_secret")


def test_settings_migrate_legacy_langfuse_host(monkeypatch, tmp_path):
    monkeypatch.delenv("LANGFUSE_BASE_URL")
    legacy_env = tmp_path / ".env"
    legacy_env.write_text("LANGFUSE_HOST=https://legacy.example.com\n", encoding="utf-8")

    settings = Settings(_env_file=legacy_env)

    assert settings.langfuse_base_url == "https://legacy.example.com"


def test_settings_reject_unknown_dotenv_keys(tmp_path):
    typo_env = tmp_path / ".env"
    typo_env.write_text("LANGFUSE_BAES_URL=https://typo.example.com\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        Settings(_env_file=typo_env)


def test_settings_parse_cors_origins_from_json_env(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        '["https://cosmos-incicrew.vercel.app","http://localhost:8123"]',
    )

    settings = Settings()

    assert settings.cors_allowed_origins == [
        "https://cosmos-incicrew.vercel.app",
        "http://localhost:8123",
    ]


def test_settings_default_to_deployed_and_local_web_origins(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS")

    settings = Settings()

    assert settings.cors_allowed_origins == [
        "https://cosmos-incicrew.vercel.app",
        "http://localhost:3000",
    ]


def test_get_settings_is_singleton():
    assert get_settings() is get_settings()
