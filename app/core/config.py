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
    google_api_key: str = ""

    # OpenAI
    openai_api_key: str = ""

    # Supabase (unused — local SQLite active; set when switching back)
    supabase_url: str = ""
    supabase_key: str = ""

    # App
    base_url: str = "https://yourdomain.com"

    # Encryption
    encryption_key: str = ""

    # Admin
    admin_secret: str = ""

    # WhatsApp App Secret (for webhook signature verification)
    whatsapp_app_secret: str = ""

    # Stripe (unused — removed for local testing)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # SendGrid (email)
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = ""

    # Twilio (SMS)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
