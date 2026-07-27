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
    # Two keys, not one: consent is incremental. Identity is requested at
    # sign-in and mailbox access only when a mailbox is connected, so nobody is
    # asked to hand over their mail before they have asked for mail ingestion.
    # Entra's consent is cumulative per user and app, so the token stored after
    # the second grant covers both.
    MS_IDENTITY_SCOPES: str = ""
    MS_MAILBOX_SCOPES: str = ""
    # No global webhook secret: each subscription carries its own random
    # `clientState`, generated at creation and stored on the row. One shared
    # value would make every tenant's notifications forgeable the moment it
    # leaked anywhere.
    MS_WEBHOOK_NOTIFICATION_URL: str = ""
    MS_WEBHOOK_LIFECYCLE_URL: str = ""

    # --- Microsoft Graph ---
    GRAPH_BASE_URL: str = ""
    GRAPH_TIMEOUT_SECONDS: float = 30.0
    # Used only when Graph throttles without a parseable Retry-After. It sends
    # one nearly always; this keeps the absence from becoming an exception.
    GRAPH_DEFAULT_RETRY_AFTER_SECONDS: float = 10.0
    # The notification endpoint is unauthenticated and public. Without a bound,
    # one request could demand a database round trip per element for as long as
    # the caller cared to make the list. Graph's own batches are far smaller.
    GRAPH_MAX_NOTIFICATIONS_PER_REQUEST: int = 200
    # What to ask for. Graph is free to grant less, and the documented maximum
    # has changed more than once — which is why nothing downstream assumes this
    # value and the renewal point is derived from what came back.
    GRAPH_SUBSCRIPTION_REQUEST_MINUTES: int = 4230
    # Renew this far into the granted lifetime. Half leaves a full half-life of
    # slack for a failed attempt and the sweep that retries it.
    #
    # Bounded to (0, 1] because `renewal_threshold` is a weighted midpoint of
    # (granted_at, expires_at): at or below 1 the renewal point always falls
    # before expiry, which is what makes a stale basis merely wasteful rather
    # than dangerous. Above 1 it lands *after* expiry and every subscription
    # lapses silently — so the bound is enforced rather than assumed.
    GRAPH_SUBSCRIPTION_RENEW_MARGIN: float = Field(default=0.5, gt=0, le=1)

    # --- Initial sync limits (plan §6.2) ---
    # Graph delta filtered by receivedDateTime is not a bulk export mechanism.
    # Whichever limit is hit first stops the walk, and the onboarding UI must
    # not offer more than these allow.
    INITIAL_SYNC_MAX_MESSAGES: int = Field(default=5000, gt=0)
    INITIAL_SYNC_MAX_LOOKBACK_DAYS: int = Field(default=90, gt=0)

    # --- Recovery sweeps (plan §8, §9) ---
    # Two grace periods, because a queue hop should be quick but a fetch or an
    # extraction legitimately takes longer. Sweeping both on the same clock
    # would duplicate work that is still in flight.
    RESCAN_PENDING_MINUTES: int = Field(default=5, gt=0)
    RESCAN_WORKING_MINUTES: int = Field(default=15, gt=0)
    RESCAN_INTERVAL_SECONDS: float = Field(default=300.0, gt=0)
    RENEW_INTERVAL_SECONDS: float = Field(default=900.0, gt=0)
    DELTA_SYNC_INTERVAL_SECONDS: float = Field(default=600.0, gt=0)
    # Hourly is enough: this catches a state that should never arise, and the
    # mailbox is still reconciled by the delta sweep meanwhile.
    ENSURE_SUBSCRIPTIONS_INTERVAL_SECONDS: float = Field(default=3600.0, gt=0)

    # --- Google sign-in (identity only — no Gmail scope; see docs/setup.md) ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""

    # --- Object storage (Cloudflare R2) ---
    # Email bodies live here rather than in Postgres: they are large, they are
    # read only by the extraction job, and retention deletes them independently
    # of the row that describes them (spec: Retention).
    R2_ENDPOINT_URL: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
    # Cloudflare's documented value. Pinned rather than left to botocore, which
    # silently defaults S3 to us-east-1 — the region is part of the SigV4
    # signature, so an ambient AWS_DEFAULT_REGION on the host would change how
    # requests are signed and R2 would reject them.
    R2_REGION: str = "auto"

    # --- AI extraction ---
    OPENROUTER_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    EXTRACTION_MODEL_FAST: str = ""
    EXTRACTION_MODEL_STRONG: str = ""

    # --- Queue (Upstash Redis) ---
    REDIS_URL: str = ""
    # arq polls, and Upstash bills per command — a tight loop costs money every
    # second the system is idle. A couple of seconds of latency is the cheaper
    # trade for a pipeline whose slowest step is an LLM call.
    ARQ_POLL_DELAY_SECONDS: float = 2.0
    ARQ_MAX_JOBS: int = 10
    ARQ_MAX_TRIES: int = 5

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
        return [s for s in self.MS_IDENTITY_SCOPES.split() if s]

    @property
    def mailbox_scopes(self) -> list[str]:
        return [s for s in self.MS_MAILBOX_SCOPES.split() if s]

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
