"""Application settings.

Every value is sourced from the environment — nothing is hardcoded. The repo
root `.env` is the single local source; Koyeb injects the same keys in
production.
"""

import ssl as ssl_module
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

# libpq connection parameters asyncpg does not accept as kwargs.
_LIBPQ_SSL_PARAMS = {"sslmode", "sslrootcert", "sslcert", "sslkey"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- App ---
    APP_ENV: str = "development"
    APP_SECRET_KEY: str
    TOKEN_ENCRYPTION_KEY: str
    FRONTEND_ORIGIN: str

    # --- Database ---
    DATABASE_URL: PostgresDsn

    # --- Microsoft Entra ID / Graph ---
    MS_CLIENT_ID: str = ""
    MS_CLIENT_SECRET: str = ""
    MS_TENANT_ID: str = "common"
    MS_REDIRECT_URI: str = ""
    MS_GRAPH_SCOPES: str = ""
    MS_WEBHOOK_CLIENT_STATE: str = ""
    MS_WEBHOOK_NOTIFICATION_URL: str = ""

    # --- AI extraction ---
    OPENROUTER_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    EXTRACTION_MODEL_FAST: str = ""
    EXTRACTION_MODEL_STRONG: str = ""

    # --- Queue ---
    REDIS_URL: str = ""

    @field_validator("MS_GRAPH_SCOPES")
    @classmethod
    def _non_empty_when_configured(cls, v: str) -> str:
        return v.strip()

    @property
    def graph_scopes(self) -> list[str]:
        return [s for s in self.MS_GRAPH_SCOPES.split() if s]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def sqlalchemy_url(self) -> str:
        """asyncpg driver URL derived from the standard postgresql:// DSN.

        libpq's `sslmode` query parameter is not an asyncpg kwarg, so it is
        stripped here and re-expressed via `asyncpg_connect_args`.
        """
        parts = urlsplit(str(self.DATABASE_URL))
        query = [(k, v) for k, v in parse_qsl(parts.query) if k not in _LIBPQ_SSL_PARAMS]
        url = urlunsplit(parts._replace(query=urlencode(query)))
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def asyncpg_connect_args(self) -> dict[str, object]:
        """Translate the DSN's libpq sslmode into asyncpg's `ssl` argument."""
        sslmode = dict(parse_qsl(urlsplit(str(self.DATABASE_URL)).query)).get("sslmode")
        if sslmode in (None, "disable"):
            return {}
        if sslmode in ("allow", "prefer", "require"):
            # Encrypt, but do not verify the server certificate — Koyeb's
            # managed Postgres presents a cert the system trust store lacks.
            ctx = ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_NONE
            return {"ssl": ctx}
        # verify-ca / verify-full: full chain and hostname verification.
        return {"ssl": ssl_module.create_default_context()}

    def microsoft_configured(self) -> bool:
        return bool(self.MS_CLIENT_ID and self.MS_CLIENT_SECRET)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
