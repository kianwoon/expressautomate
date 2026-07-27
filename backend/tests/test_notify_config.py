"""Notification settings load from the environment, never from source."""

from app.core.config import Settings, settings


def test_notify_defaults_are_present() -> None:
    assert settings.NOTIFY_RATE_CAP_PER_HOUR > 0
    assert settings.NOTIFY_LINK_TOKEN_TTL_MINUTES > 0
    assert settings.NOTIFY_MAX_ATTEMPTS > 0
    assert settings.NOTIFY_MAX_FAILURES > 0
    assert settings.NOTIFY_OPT_IN_MAX_PER_HOUR > 0


def test_channels_report_unconfigured_when_credentials_are_absent() -> None:
    """An empty token must read as 'not configured', not as a usable client."""
    blank = Settings(
        APP_SECRET_KEY="x",
        TOKEN_ENCRYPTION_KEY="x",
        FRONTEND_ORIGIN="http://localhost:3000",
        DATABASE_URL="postgresql://u:p@localhost/db",
        TELEGRAM_BOT_TOKEN="",
        WHATSAPP_ACCESS_TOKEN="",
        WHATSAPP_PHONE_NUMBER_ID="",
    )
    assert blank.telegram_configured() is False
    assert blank.whatsapp_configured() is False


def test_channels_report_configured_when_credentials_are_present() -> None:
    ready = Settings(
        APP_SECRET_KEY="x",
        TOKEN_ENCRYPTION_KEY="x",
        FRONTEND_ORIGIN="http://localhost:3000",
        DATABASE_URL="postgresql://u:p@localhost/db",
        TELEGRAM_BOT_TOKEN="bot-token",
        TELEGRAM_API_BASE_URL="https://api.telegram.org",
        WHATSAPP_ACCESS_TOKEN="wa-token",
        WHATSAPP_PHONE_NUMBER_ID="1234567890",
        WHATSAPP_API_BASE_URL="https://graph.facebook.com/v21.0",
    )
    assert ready.telegram_configured() is True
    assert ready.whatsapp_configured() is True
