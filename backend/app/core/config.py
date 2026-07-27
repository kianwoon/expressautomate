"""Application settings.

Every value is sourced from the environment — nothing is hardcoded. The repo
root `.env` is the single local source; Koyeb injects the same keys in
production.
"""

import ssl as ssl_module
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import PostgresDsn, field_validator
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
    SQL_ECHO: bool = False
    # Koyeb strips the matched route prefix before forwarding, so a request to
    # https://expressautomate.app/api/early-access arrives here as
    # /early-access. Routes are therefore declared unprefixed, and root_path
    # tells FastAPI the public prefix so /docs and openapi.json emit correct
    # URLs. Empty locally, where nothing sits in front.
    API_ROOT_PATH: str = ""
    APP_SECRET_KEY: str
    TOKEN_ENCRYPTION_KEY: str
    FRONTEND_ORIGIN: str

    # --- Database ---
    # DATABASE_URL is the runtime connection and MUST use a role without
    # BYPASSRLS. DATABASE_ADMIN_URL owns the schema and is used only by
    # Alembic — see app/db/rls.py.
    DATABASE_URL: PostgresDsn
    DATABASE_ADMIN_URL: PostgresDsn | None = None
    DATABASE_APP_ROLE: str = "expressautomate_app"
    DATABASE_APP_PASSWORD: str = ""

    # --- Microsoft Entra ID / Graph ---
    MS_CLIENT_ID: str = ""
    MS_CLIENT_SECRET: str = ""
    # `common`, deliberately: it admits work/school *and* personal Microsoft
    # accounts, both of which can sign in. Personal accounts all report one
    # shared MSA tenant GUID, so they are never keyed on it — `_tenant_for` in
    # app/api/auth.py derives a private per-user tenant from the `oid` claim
    # instead, which is what keeps them from reading each other's rows.
    MS_TENANT_ID: str = "common"
    MS_REDIRECT_URI: str = ""
    # Two scope sets, not one, because they face very different consent bars
    # (§6.1). Asking for both at sign-in is what locked a real agency out:
    # Microsoft's recommended tenant policy lets users consent only to
    # "low impact" permissions, and mailbox access is not one — so bundling
    # them made *signing in at all* need an administrator.
    #
    # MS_IDENTITY_SCOPES is what sign-in asks for: enough to know who someone
    # is, and nothing a cautious tenant would refuse.
    # MS_MAILBOX_SCOPES is asked for separately, later, by someone who has
    # chosen to connect a mailbox — and may still need an admin, which is a
    # far better place to meet that wall than the front door.
    MS_IDENTITY_SCOPES: str = ""
    MS_MAILBOX_SCOPES: str = ""
    MS_WEBHOOK_CLIENT_STATE: str = ""
    MS_WEBHOOK_NOTIFICATION_URL: str = ""

    # --- Google sign-in (identity only — no Gmail scope; see docs/setup.md) ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""

    # --- AI extraction ---
    OPENROUTER_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    EXTRACTION_MODEL_FAST: str = ""
    EXTRACTION_MODEL_STRONG: str = ""

    # --- Queue ---
    REDIS_URL: str = ""

    @field_validator("MS_IDENTITY_SCOPES", "MS_MAILBOX_SCOPES")
    @classmethod
    def _non_empty_when_configured(cls, v: str) -> str:
        return v.strip()

    @field_validator("API_ROOT_PATH")
    @classmethod
    def _normalise_root_path(cls, v: str) -> str:
        """Force a leading slash and no trailing one.

        `/api/` would make the OpenAPI URL `/api//openapi.json`, which the
        proxy strips to `//openapi.json` and nothing serves — routing still
        works, so only /docs quietly breaks. Stripping both ends also closes
        `//api`, which a browser reads as a scheme-relative URL to host `api`:
        the same broken /docs, reached a different way.
        """
        v = v.strip().strip("/")
        return f"/{v}" if v else ""

    @property
    def identity_scopes(self) -> list[str]:
        """What sign-in asks for."""
        return [s for s in self.MS_IDENTITY_SCOPES.split() if s]

    @property
    def mailbox_scopes(self) -> list[str]:
        """The extra permissions mailbox ingestion needs, asked for separately."""
        return [s for s in self.MS_MAILBOX_SCOPES.split() if s]

    @property
    def graph_scopes(self) -> list[str]:
        """Everything a fully connected user has granted.

        Order matters only in that it is stable: this is what the mailbox
        consent flow requests. It re-asks for the identity scopes too, because
        an incremental consent that named only the new permission would return
        a token narrower than the one already held.
        """
        seen = dict.fromkeys(self.identity_scopes + self.mailbox_scopes)
        return list(seen)

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def sqlalchemy_url(self) -> str:
        """asyncpg driver URL derived from the standard postgresql:// DSN.

        libpq's `sslmode` query parameter is not an asyncpg kwarg, so it is
        stripped here and re-expressed via `asyncpg_connect_args`.
        """
        return self._to_asyncpg(str(self.DATABASE_URL))

    @property
    def alembic_url(self) -> str:
        """Migrations run as the schema owner, not the RLS-bound runtime role."""
        dsn = str(self.DATABASE_ADMIN_URL or self.DATABASE_URL)
        return self._to_asyncpg(dsn)

    @staticmethod
    def _to_asyncpg(dsn: str) -> str:
        parts = urlsplit(dsn)
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
        """Identity *and* the only path to mailbox ingestion (§6.1)."""
        return bool(self.MS_CLIENT_ID and self.MS_CLIENT_SECRET)

    def google_configured(self) -> bool:
        """Identity only — Google users have no mailbox to ingest."""
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
