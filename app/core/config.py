"""Centralised configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # WhatsApp (Meta)
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "https://yourdomain.com/auth/google/callback"

    # OpenAI
    openai_api_key: str = ""

    # Supabase (unused — local SQLite active; set when switching back)
    supabase_url: str = ""
    supabase_key: str = ""

    # App
    base_url: str = "https://yourdomain.com"

    # Encryption
    encryption_key: str = ""

    # Stripe (unused — removed for local testing)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
