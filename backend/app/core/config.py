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
    # Unused. Each subscription generates its own random `clientState` at
    # creation and stores it on the row, so there is no shared webhook secret
    # to leak — removing the key is a separate change from this merge.
    MS_WEBHOOK_CLIENT_STATE: str = ""
    MS_WEBHOOK_NOTIFICATION_URL: str = ""
    MS_WEBHOOK_LIFECYCLE_URL: str = ""
    # A separate redirect for the mailbox consent. Sharing the sign-in callback
    # would land the mailbox grant on a handler that creates a session instead
    # of a mailbox. Must be registered in the Entra app registration too —
    # Entra rejects any redirect it has not been told about.
    MS_MAILBOX_REDIRECT_URI: str = ""
    # How long a tenant keeps source email by default (spec: Retention).
    DEFAULT_RETENTION_MONTHS: int = Field(default=24, gt=0)

    # --- Microsoft Graph ---
    # Empty is a real deployment state, not a default worth using: httpx then
    # builds a URL with no host and fails deep inside the transport, long past
    # anything that could explain it. That is precisely how this shipped — the
    # web service went live without it because, until the inbox preview, only
    # the workers ever called Graph. `graph_configured()` is what makes the
    # absence answerable at the edge instead of a 500 in a traceback.
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
    # How much further back an *extension* must reach before we will re-run the
    # backfill for it. It exists only to stop a nudge: a start date is a fixed
    # instant while every option is measured from `now`, so an option chosen
    # yesterday drifts a day "earlier" overnight and would be offered back as
    # an extension worth a few hours of mail and a full re-walk to get it.
    #
    # One day, not seven. Seven silently removed real choices: a mailbox
    # started on the 26th could not be extended to "last 7 days" on the 28th,
    # because that reaches only five days further back — a legitimate request
    # that simply vanished from the page with nothing to say why.
    #
    # This is only the floor for the shortest windows. The test that does the
    # real work is the fraction below; a flat day count cannot tell "five days
    # gained on a seven-day walk" from "one day gained on a ninety-day walk",
    # and those deserve opposite answers.
    LOOKBACK_EXTENSION_MIN_DAYS: int = Field(default=1, gt=0)
    # What share of the walk has to be new history for it to be worth running.
    # A tenth: reaching 90 days must gain at least nine, reaching seven must
    # gain at least one. Without it, a mailbox that chose 30 days two months
    # ago would be offered a full ninety-day re-walk in exchange for a single
    # day of older mail — technically an extension, and thousands of Graph
    # calls for nothing anybody would notice.
    LOOKBACK_EXTENSION_MIN_FRACTION: float = Field(default=0.1, gt=0, le=1)

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

    # --- Candidate avatar photos (shown only inside the candidate modal) ---
    AVATAR_MAX_UPLOAD_BYTES: int = Field(default=5 * 1024 * 1024, gt=0)
    AVATAR_MAX_PIXEL_DIMENSION: int = Field(default=1024, gt=0)
    AVATAR_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
    # What every avatar is re-encoded *to*. Whatever the client uploads is
    # decoded and written back out in this format, which is what strips EXIF
    # (phone photos carry GPS). A Pillow format name, not a MIME type: the MIME
    # type is looked up from Pillow's own registry so the two can never drift.
    # PNG by default because it is lossless and keeps transparency.
    AVATAR_STORED_FORMAT: str = "PNG"

    # --- CV documents ---
    # How much text one CV may contribute. This is the decompression-bomb
    # bound `app.services.cv.text` enforces on our behalf, and it is also the
    # size of the string every evidence offset indexes into, so it belongs in
    # configuration rather than at the call site: a deployment that raises it
    # is accepting both a longer prompt and a larger worker footprint.
    CV_TEXT_MAX_CHARS: int = Field(default=200_000, gt=0)
    # The wall clock a single CV parse may occupy an arq worker for, model
    # call included. `text.py` bounds DOCX inflation and stops PDF text from
    # accumulating between pages, but a single-page FlateDecode bomb still
    # inflates inside `pypdf` where nothing is watching. This is the only
    # thing standing between one hostile page and a worker slot held forever.
    CV_PARSE_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0)
    # The largest upload the API will accept, counted as the bytes arrive
    # rather than trusted from `Content-Length`. Generous next to a real CV
    # and far below `CV_TEXT_MAX_CHARS`, so the bound that actually bites a
    # hostile file is the text one, not this.
    CV_MAX_UPLOAD_BYTES: int = Field(default=10 * 1024 * 1024, gt=0)
    # How long a download link stays valid. Short for the same reason the
    # avatar's is: a signed URL is a capability, and one that outlives the
    # session it was minted under is a capability nobody revoked.
    CV_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
    # How many CVs one agency may upload in a UTC day. Each upload buys a
    # model call, so this is a spend ceiling before it is anything else.
    CV_DAILY_PARSE_QUOTA: int = Field(default=200, gt=0)

    # --- Candidate spreadsheet imports ---
    # The largest spreadsheet the API will accept, counted as the bytes
    # arrive rather than trusted from `Content-Length`. Larger than a CV
    # because a five-hundred-person roster with a career history beside it is
    # the ordinary case here, not the pathological one.
    IMPORT_MAX_UPLOAD_BYTES: int = Field(default=20 * 1024 * 1024, gt=0)
    # How many data rows one sheet may hold. This is the bound that stops an
    # import building an unbounded list of dicts in a worker's memory, and it
    # is quoted back to the recruiter when a file exceeds it, so it belongs in
    # configuration rather than at the call site.
    IMPORT_MAX_ROWS: int = Field(default=5_000, gt=0)
    # How much an uploaded XLSX's zip members may inflate to in total. An
    # XLSX is a decompression-bomb vector in exactly the way a DOCX is: a
    # member's declared uncompressed size is a claim the file makes about
    # itself. `read_sheets` inflates against this budget instead of believing
    # it.
    IMPORT_INFLATE_BUDGET_BYTES: int = Field(default=200 * 1024 * 1024, gt=0)
    # How long a link to an import's error report stays valid. Short for the
    # same reason a CV's is: a signed URL is a capability, and the report
    # names real candidates.
    IMPORT_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
    # The wall clock one import may occupy an arq worker for. An import is
    # database work rather than a model call, but it is database work whose
    # size the uploader chooses, and `rescan_stuck` re-enqueues a run this
    # cuts short — so the cost of a genuinely huge file is a retry, not a
    # worker slot held for the life of the process.
    IMPORT_JOB_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)
    # How many times a worker may pick one import up before giving up on it.
    # `rescan_stuck` re-enqueues any import left non-terminal, which is the
    # right answer to a crashed worker or a database outage and the wrong one
    # to a file that crashes the apply every single time — that pair loops for
    # ever, burning a worker slot per sweep and telling nobody. Above one so a
    # genuinely transient failure still gets its retry.
    IMPORT_MAX_ATTEMPTS: int = Field(default=3, gt=0)

    # --- AI extraction ---
    # Kept although nothing calls the router any more: OPENROUTER_API_KEY and
    # LLM_BASE_URL are still what `llm_configured` answers for, and a deployment
    # that moves extraction back to a router needs them present rather than
    # rediscovered.
    OPENROUTER_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    EXTRACTION_MODEL_FAST: str = ""
    EXTRACTION_MODEL_STRONG: str = ""
    CLASSIFIER_MODEL: str = ""

    # --- Cerebras (the classifier and extraction) ---
    # The gate is the highest-volume call in the system — one per email, on
    # every email, forever — so it runs on its own provider rather than through
    # the router the extraction calls use. Measured at ~36ms round trip against
    # gpt-oss-120b, which matters because the gate sits between a fetched email
    # and everything else.
    #
    # Extraction joined it after the router path failed in production, not on
    # principle. The escalation model (§32) rejected our twelve-field schema
    # outright — "the compiled grammar is too large" — and every real email sat
    # at `extracting` with nothing to show for it. The same document, sent as
    # prompt text with a plain `json_object` response format, is answered
    # correctly here in ~1.5s and at a fraction of the price.
    CEREBRAS_BASE_URL: str = ""
    CEREBRAS_API_KEY: str = ""
    # How many emails one classification call covers. Batching is the whole
    # cost saving here: the per-call overhead (system prompt, instructions) is
    # paid once for the batch rather than once per email.
    #
    # Bounded because a batch is also a blast radius: one malformed response
    # costs every email in it a retry, and a long prompt is likelier to have
    # the model lose track of which verdict belongs to which message.
    CLASSIFIER_BATCH_SIZE: int = Field(default=20, gt=0, le=100)
    # Characters of each email shown to the gate. It answers "is this a job
    # order", which the opening of a message settles; sending the whole body
    # would multiply the cost of the cheap stage for no better answer.
    CLASSIFIER_CHARS_PER_EMAIL: int = Field(default=1200, gt=0)
    # Reasoning models spend this budget before emitting anything. Set too low,
    # `gpt-oss-120b` returns a `reasoning` field and no `content` at all —
    # verified, not theorised — so this must leave room for both.
    CLASSIFIER_MAX_TOKENS: int = Field(default=4000, gt=0)
    # How often the supervisor sweeps for `fetched` rows to classify. This is
    # the latency an email waits before the gate sees it at all, now that
    # `fetch_email` no longer enqueues a per-email job: batching trades a
    # little delay for the per-call overhead paid once instead of once each.
    # `gpt-oss-120b` reasons before it answers, and at the default effort it
    # spends the whole budget doing so and returns no content — verified
    # against the live API, not theorised. Configurable because the right
    # answer is a property of the model, and the model is configurable.
    CLASSIFIER_REASONING_EFFORT: str = "low"
    CLASSIFY_SWEEP_INTERVAL_SECONDS: float = Field(default=30.0, gt=0)
    # Ceiling on how many rows one sweep claims, across every tenant. Without
    # it a backfill of ten thousand messages would be claimed in a single tick
    # and enqueued as hundreds of batches at once, and every one of them would
    # be in flight — at `classifying` — before the first had answered.
    CLASSIFY_SWEEP_LIMIT: int = Field(default=200, gt=0)
    # Generous next to GRAPH_TIMEOUT_SECONDS on purpose: a long recruitment
    # email on a strong model routinely spends a minute generating, and a
    # timeout here costs the whole extraction plus a retry's worth of tokens.
    LLM_TIMEOUT_SECONDS: float = 90.0
    # An extraction answers for twelve fields per vacancy and quotes the email
    # for each, so its completions are an order of magnitude longer than the
    # gate's one-word verdicts. Too low and the response is truncated mid-JSON,
    # which arrives as `LLMInvalidJSON` and looks like a model problem.
    EXTRACTION_MAX_TOKENS: int = Field(default=16000, gt=0)
    # Escalation (§32) is a change of effort, not a change of provider. The
    # obvious escalation — a bigger model behind a router — is what broke
    # extraction in production, and a second model is only safe once someone
    # has verified it accepts this request. Reasoning effort needs no such
    # verification: it is the same endpoint, the same response format, the same
    # model that already answered, thinking longer. There is no request shape
    # here that can 400 when the first call did not.
    EXTRACTION_REASONING_EFFORT_FAST: str = "low"
    EXTRACTION_REASONING_EFFORT_STRONG: str = "high"
    # Stamped onto every extraction so a prompt change is attributable — without
    # it, a quality regression cannot be told from a change in the mail itself.
    PROMPT_VERSION: str = "v1"
    # The self-reported confidence a fully verified extraction must clear to be
    # called `verified` rather than `likely`. Tunable because the right number
    # is a property of the model, not of this code: swapping the extraction
    # model recalibrates what a 0.8 means, and a threshold frozen in source
    # would silently send every row to review or none of them. It can only
    # demote — it never rescues a span that failed verification.
    EXTRACTION_VERIFIED_CONFIDENCE: float = Field(default=0.8, ge=0.0, le=1.0)
    # A message the gate rejected yields nothing, so keeping it for the full
    # tenant horizon is stored personal data with no purpose left to justify it
    # (spec: Retention). Short rather than zero: an operator disputing a wrong
    # verdict needs the source still there to look at.
    NON_RECRUITMENT_RETENTION_DAYS: int = Field(default=7, gt=0)
    # The only currency codes a salary may be filed under. Anything else is
    # dropped rather than guessed: a bare `[A-Z]{3}` scan read "KLN pays 3500"
    # as currency KLN. Which codes are real is a property of the market a
    # deployment serves, not of this code — Singapore sees these.
    SALARY_CURRENCY_CODES: str = "SGD,USD,MYR,EUR,GBP,AUD,HKD,CNY,JPY,INR,PHP,IDR,THB"
    # Symbol-to-code map. A bare "$" is deliberately absent: in Singapore mail
    # it is SGD or USD depending on the sender, and picking one would file a
    # 30% error as a fact.
    SALARY_CURRENCY_SYMBOLS: str = "S$=SGD,RM=MYR,€=EUR,£=GBP,¥=JPY,₹=INR"
    # Plausibility window for a parsed salary figure. Outside it the number is
    # something else the sentence mentioned — working hours, a headcount, a
    # postcode — and the whole string is refused rather than half-read. Both
    # bounds are per-period and market-specific, hence configurable.
    SALARY_MIN_CREDIBLE: float = Field(default=100.0, gt=0)
    SALARY_MAX_CREDIBLE: float = Field(default=10_000_000.0, gt=0)
    # How many job orders one dashboard request returns. The table renders
    # every row it is given, so this is the ceiling on what a browser is asked
    # to lay out — an operational limit, which is why it is not a literal in
    # the endpoint. Newest first, so the cap trims the oldest.
    OPPORTUNITIES_PAGE_LIMIT: int = Field(default=200, gt=0)

    CLIENTS_PAGE_LIMIT: int = Field(default=200, gt=0)

    CANDIDATES_PAGE_LIMIT: int = Field(default=200, gt=0)

    # How many WhatsApp-open activities the candidate history panel returns.
    # Newest first, same reasoning as CANDIDATES_PAGE_LIMIT.
    CANDIDATE_ACTIVITIES_PAGE_LIMIT: int = Field(default=200, gt=0)

    # The most years of experience a candidate record may claim. There is no
    # database constraint to derive this from, and none is wanted: the point of
    # the bound is to catch a mistyped figure (a birth year in the experience
    # box) before it reaches the row, not to legislate a career length. So it
    # is deliberately generous — longer than any working life — and configurable
    # rather than a literal in the endpoint.
    CANDIDATE_MAX_YEARS_EXPERIENCE: int = Field(default=80, gt=0)

    # Phone numbers are parsed to E.164 before they identify anyone. A sheet
    # writes "9123 4567" and means +65 9123 4567; without a region there is
    # nothing to resolve that against.
    DEFAULT_PHONE_REGION: str = Field(default="SG", min_length=2, max_length=2)

    # Which leading digits belong to a person rather than a switchboard. A
    # fixed line is shared by a whole company, so matching a candidate on one
    # would merge colleagues into a single record.
    MOBILE_PREFIXES_RAW: str = Field(default="8,9", alias="MOBILE_PREFIXES")

    @property
    def MOBILE_PREFIXES(self) -> frozenset[str]:
        return frozenset(
            part.strip() for part in self.MOBILE_PREFIXES_RAW.split(",") if part.strip()
        )

    # Which domains may never key a client. A hiring manager writing from
    # gmail.com identifies a person, not a company, and matching on that
    # domain would file every unrelated agency's clients under one row.
    # Stored as a comma-separated string in the environment and split here so
    # the operator can extend it without a code change.
    FREE_EMAIL_DOMAINS_RAW: str = Field(
        default="gmail.com,googlemail.com,hotmail.com,outlook.com,live.com,"
        "yahoo.com,yahoo.com.sg,icloud.com,me.com,proton.me,protonmail.com,"
        "aol.com,qq.com,163.com",
        alias="FREE_EMAIL_DOMAINS",
    )

    @property
    def FREE_EMAIL_DOMAINS(self) -> frozenset[str]:
        """Parsed once per access; the raw string is what the environment sets."""
        return frozenset(
            part.strip().lower()
            for part in self.FREE_EMAIL_DOMAINS_RAW.split(",")
            if part.strip()
        )

    # --- Client shorthand glossary ---
    # Whether a tenant reading its glossary for the first time is offered the
    # shipped starter codes. On by default, because an empty glossary on day
    # one teaches nothing about what the feature is for. Switchable because a
    # deployment outside Singapore inherits shorthand that is simply wrong
    # there, and being handed twenty wrong defaults is worse than being handed
    # none. Turning it off never removes codes already seeded — the seed ledger
    # keeps those decisions with the agency.
    GLOSSARY_SEED_STARTERS: bool = True
    # Ceilings on what one glossary row may hold. The code is shorthand, not
    # prose, and the meaning is a line of explanation the UI puts in a table
    # cell; both are here rather than as literals in the endpoint so a
    # deployment can loosen them without a code change.
    GLOSSARY_CODE_MAX_LENGTH: int = Field(default=32, gt=0, le=64)
    GLOSSARY_MEANING_MAX_LENGTH: int = Field(default=500, gt=0)
    # --- Glossary scanning (app/services/ingest/glossary.py) ---
    # The shortest code the scanner will look for. A one-character entry
    # matches somewhere in almost every email, so decoding it would decorate
    # every job order with a demographic claim the client never made; an agency
    # that writes `M` in its glossary almost certainly meant it as a note.
    # Configurable because how short is too short depends on the shorthand a
    # market actually uses, not on this code.
    GLOSSARY_MIN_CODE_LENGTH: int = Field(default=2, gt=0)
    # Punctuation that may legally sit against a code without making it part of
    # a longer token. Everything absent from this set — `/`, `-`, letters,
    # digits — means the real token is longer than what matched, and reporting
    # the fragment would report a code the email does not contain. This is the
    # rule that keeps `C/F` out of `ABC/FGH`, so it is a deployment setting
    # rather than a literal: adding a character here widens what gets decoded.
    GLOSSARY_BOUNDARY_CHARS: str = "(),;:!?\"'“”‘’[]{}<>*—–…"
    # Punctuation that ends a code only when nothing alphanumeric follows it.
    # A full stop is genuinely both things: "we want C/F." closes a sentence,
    # while "C.F.M" is one three-part code whose first two thirds must not be
    # decoded on their own. The far side of the character decides which it was.
    GLOSSARY_WEAK_BOUNDARY_CHARS: str = "."
    # How many glossary rows one scan will compile patterns for. A tenant that
    # pastes a thousand codes would otherwise make every extraction pay for
    # them; the cap bounds the per-email cost rather than the glossary itself.
    GLOSSARY_MAX_CODES: int = Field(default=500, gt=0)

    # --- Sync activity log ---
    # How many events one mailbox keeps. The bound is enforced by the writer,
    # which trims in the same transaction as the insert, rather than by a purge
    # on a timer: a mailbox syncing every few minutes writes faster than any
    # schedule a purge could reasonably run on, so a timer-based sweep decides
    # how far the table overshoots rather than whether it does.
    SYNC_ACTIVITY_KEEP_PER_MAILBOX: int = Field(default=200, gt=0)
    # How many events the dashboard panel shows, newest first. Smaller than the
    # retained history on purpose — the panel answers "did the last sync work",
    # and the rest is there for someone investigating afterwards.
    SYNC_ACTIVITY_PAGE_LIMIT: int = Field(default=50, gt=0)

    # --- Queue (Upstash Redis) ---
    REDIS_URL: str = ""
    # arq polls, and Upstash bills per command — a tight loop costs money every
    # second the system is idle. A couple of seconds of latency is the cheaper
    # trade for a pipeline whose slowest step is an LLM call.
    ARQ_POLL_DELAY_SECONDS: float = 2.0
    ARQ_MAX_JOBS: int = 10
    ARQ_MAX_TRIES: int = 5

    # --- Dashboard live updates (SSE over Redis pub/sub) ---
    # Channel names are namespaced so one Redis can serve more than one
    # environment without staging nudging production's dashboards. The tenant
    # id is appended per channel — see `app.services.events.channel_for`.
    EVENTS_CHANNEL_PREFIX: str = "ea:events:"
    # How long a stream may stay silent before it sends a keep-alive comment.
    # Koyeb's proxy closes an idle connection, and the browser cannot tell that
    # from a genuinely quiet mailbox, so it would reconnect on a loop. Kept
    # comfortably under any proxy idle timeout worth guessing at.
    EVENTS_HEARTBEAT_SECONDS: float = Field(default=20.0, gt=0)

    # --- Notifications (spec 2026-07-28) ---
    # Blank by default. A channel with no credentials is *skipped*, not an
    # error: the platform must boot and ingest mail before either provider is
    # provisioned, and a missing token discovered at startup is far cheaper
    # than one discovered inside a worker on the far side of the queue.
    TELEGRAM_BOT_TOKEN: str = ""
    # The public @name, used to build the t.me deep link. Not derivable from
    # the token, and a wrong one produces a link to somebody else's bot.
    TELEGRAM_BOT_USERNAME: str = ""
    TELEGRAM_API_BASE_URL: str = ""

    # --- WA gateway (Baileys, per-recruiter outbound WhatsApp) ---
    # Distinct from TELEGRAM_*/WHATSAPP_* above on purpose (spec
    # 2026-07-29-baileys-gateway-plan.md, "Where the gateway lives"): those
    # names belong to the existing Meta Cloud API notification channel
    # (`whatsapp_webhook.py`), which this code never touches. `WA_GATEWAY_*`
    # is the one prefix the Baileys build is allowed to use anywhere.
    #
    # Empty by default for the same reason GRAPH_BASE_URL is: this is the
    # first call `api` makes to the gateway service, and CLAUDE.md records two
    # outages (GRAPH_BASE_URL, R2_*) from a service's first external call
    # going out with an unset env var. `wa_gateway_configured()` below is what
    # makes the absence answerable at the edge instead of an httpx error deep
    # in a traceback.
    WA_GATEWAY_URL: str = ""
    # Presented as `Authorization: Bearer …` on every call to the gateway, and
    # required identically on the gateway's own `WA_GATEWAY_SHARED_SECRET`
    # (gateway/src/config.ts) — the two must be set to the same value on
    # Koyeb. Also the credential the gateway uses to authenticate ITS calls
    # back to `POST /api/wa/internal/status`, so a wrong or missing value
    # breaks both directions identically rather than one silently.
    WA_GATEWAY_SHARED_SECRET: str = ""
    # Short: every WA gateway route is called synchronously from a browser
    # request (pairing, a status refetch, a disconnect click), so a slow
    # gateway should fail fast into `gateway_unreachable` rather than hold the
    # request open. Baileys' own reconnect logic runs independently on the
    # gateway side and is unaffected by this timeout.
    WA_GATEWAY_TIMEOUT_SECONDS: float = 5.0
    # How many messages one recruiter may send through their own paired
    # WhatsApp per UTC day. Plan §9's ban-risk mitigation, and the reason it
    # is a per-user counter rather than per-tenant: WhatsApp bans the *number*,
    # and each recruiter pairs their own. Low by default on purpose — a
    # personal account that suddenly sends hundreds is the exact pattern
    # WhatsApp acts on, and a cap that has to be raised deliberately is safer
    # than one discovered after a ban. Spacing and jitter between sends are a
    # separate concern (P5); this is only the daily ceiling.
    WA_SEND_DAILY_LIMIT: int = 50
    # Telegram echoes this in `X-Telegram-Bot-Api-Secret-Token`. Without it the
    # webhook accepts anything that can reach the URL, and the URL is public.
    TELEGRAM_WEBHOOK_SECRET: str = ""

    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_API_BASE_URL: str = ""
    # Meta signs webhook bodies with this; it is the app secret, not the token.
    WHATSAPP_APP_SECRET: str = ""
    # Echoed back during Meta's one-time webhook verification handshake.
    WHATSAPP_VERIFY_TOKEN: str = ""

    # Template *names*, not bodies. Meta's approval cycle can rename or
    # re-version a template with no deploy on our side; a name compiled into
    # source would need one, and the failure is a silent non-delivery.
    WHATSAPP_TEMPLATE_OPPORTUNITY_NEW: str = ""
    WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW: str = ""
    WHATSAPP_TEMPLATE_LINK_CODE: str = ""
    WHATSAPP_TEMPLATE_LANG: str = "en"

    # A forty-vacancy morning is forty billable WhatsApp messages otherwise.
    NOTIFY_RATE_CAP_PER_HOUR: int = Field(default=6, gt=0)
    NOTIFY_LINK_TOKEN_TTL_MINUTES: int = Field(default=15, gt=0)
    NOTIFY_MAX_ATTEMPTS: int = Field(default=5, gt=0)
    NOTIFY_MAX_FAILURES: int = Field(default=3, gt=0)
    # Sending an authentication template to any number a user types is an
    # OTP pump aimed at our WABA's reputation. This is the ceiling per user.
    NOTIFY_OPT_IN_MAX_PER_HOUR: int = Field(default=5, gt=0)
    # The six-digit WhatsApp code is guessable in ~1e6 tries; RLS keeps a
    # guess from reaching another tenant's code, but a same-tenant actor can
    # still brute-force a colleague's live code within its TTL. This caps
    # verify *attempts* per user the way NOTIFY_OPT_IN_MAX_PER_HOUR caps
    # code *requests* — higher than that ceiling because legitimate retries
    # (typos) are more common here than legitimate re-requests are there.
    NOTIFY_VERIFY_MAX_PER_HOUR: int = Field(default=10, gt=0)
    # The window the counter above is measured over. Kept as its own setting
    # rather than a literal 3600 in linking.py so the "per hour" in the name
    # above stays true even if the window is ever tuned.
    NOTIFY_VERIFY_WINDOW_SECONDS: int = Field(default=3600, gt=0)
    NOTIFY_SWEEP_INTERVAL_SECONDS: float = Field(default=300.0, gt=0)
    # How long a delivery may sit `pending` before the sweep assumes its
    # enqueue was lost. Must exceed the worst realistic queue latency, or the
    # sweep competes with a job that is merely slow.
    NOTIFY_DELIVERY_STALE_MINUTES: int = Field(default=10, gt=0)
    # Bounds one tick's work, so a backlog drains steadily instead of queueing
    # ten thousand jobs in a single sweep.
    NOTIFY_FLUSH_LIMIT: int = Field(default=200, gt=0)

    # How much each signal counts when ranking a candidate against a job order.
    # These are the knobs an agency owner will want to turn — one desk lives on
    # skills, another on who the person already worked for — so they are
    # settings rather than numbers buried in `services/sourcing/score.py`.
    # Only their ratios matter: the total is a weighted *mean* over the
    # components that had data, so the set never has to sum to anything.
    SOURCING_WEIGHT_TITLE: float = Field(default=3.0, ge=0)
    SOURCING_WEIGHT_SKILLS: float = Field(default=3.0, ge=0)
    SOURCING_WEIGHT_EMPLOYER: float = Field(default=1.0, ge=0)
    SOURCING_WEIGHT_SALARY: float = Field(default=2.0, ge=0)
    SOURCING_WEIGHT_TENURE: float = Field(default=1.0, ge=0)
    SOURCING_WEIGHT_RECENCY: float = Field(default=1.0, ge=0)

    # The experience at which the tenure signal is already full marks. Beyond
    # it more years say nothing further about this job, and letting them keep
    # climbing would rank a thirty-year career above a well-matched ten-year
    # one on tenure alone. Months, because early-career candidates are exactly
    # where the resolution is needed.
    SOURCING_TENURE_FULL_MONTHS: int = Field(default=120, gt=0)
    # How long a candidate's most recent role may have ended before the
    # recency signal reaches zero. This measures distance from the workforce,
    # not age — a career break and a young candidate look identical to it, and
    # that is the point.
    SOURCING_RECENCY_STALE_MONTHS: int = Field(default=36, gt=0)
    # Scores are compared, stored and shown; rounding them at one agreed place
    # is what makes "the same inputs give the same score" true of the value a
    # recruiter reads rather than only of the arithmetic behind it.
    SOURCING_SCORE_DECIMAL_PLACES: int = Field(default=4, ge=0, le=6)
    # How many of the highest-scoring candidates are sent to a model to be
    # explained. Every one of them costs tokens and CV text in a prompt, and a
    # recruiter reads the top of a shortlist, not the tail of it — so this is
    # the knob that decides what the feature costs per run.
    SOURCING_EXPLAIN_TOP_N: int = Field(default=10, gt=0)
    # How many matches a run keeps. Scoring looks at every eligible candidate
    # in the agency, and an agency with two thousand of them would otherwise
    # get two thousand rows written, serialised and rendered — a data dump
    # rather than a shortlist. The cap belongs to the run because that is
    # where "the best of what we scored" is still known; a reader who trims
    # afterwards is only hiding rows that were already paid for.
    SOURCING_MAX_MATCHES: int = Field(default=20, gt=0)
    # The wall clock one sourcing run may occupy an arq worker for. A run
    # scores every eligible candidate in the tenant and then spends a model
    # call on the top of that list, so its size is the agency's database
    # rather than anything the caller chose — and a run this cuts short is
    # left at `running` for `rescan_stuck` to re-enqueue.
    SOURCING_JOB_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)
    # How many times a worker may pick one run up before giving up on it.
    # `rescan_stuck` re-enqueues any run left non-terminal, which is the right
    # answer to a crashed worker and the wrong one to a job order that crashes
    # the scorer every single time — that pair loops for ever, burning a
    # worker slot per sweep and a model call with it. Above one so a genuinely
    # transient failure still gets its retry.
    SOURCING_MAX_ATTEMPTS: int = Field(default=3, gt=0)
    # How many runs one agency may start in a UTC day. Each run buys a model
    # call over the top of its shortlist, so this is a spend ceiling before it
    # is anything else — the same reason `CV_DAILY_PARSE_QUOTA` exists.
    SOURCING_DAILY_RUN_QUOTA: int = Field(default=100, gt=0)

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

    def llm_configured(self, *models: str) -> bool:
        """Can this process actually reach the named models?

        Same shape as `graph_configured`, and for the same reason: an empty
        base URL makes httpx build a hostless URL and raise `unknown url type`
        deep in the stack, naming nothing.

        The model ids are checked too, and that is not fussiness. They default
        to `""`, and an empty model id is not a missing argument — it is a
        request the provider rejects, per email, with a message about the model
        rather than about the configuration. This deployment reached the point
        of classifying real mail with `CLASSIFIER_MODEL` set nowhere at all.
        """
        if not (self.LLM_BASE_URL and self.OPENROUTER_API_KEY):
            return False
        return all(models)

    def cerebras_configured(self, *models: str) -> bool:
        """The same question as `llm_configured`, asked of the gate's provider.

        Kept separate rather than adding a flag to `llm_configured`: the two
        answer for different credentials, and a single function returning True
        because the *other* provider was configured is exactly how the gate
        would end up classifying real mail against a hostless URL.
        """
        if not (self.CEREBRAS_BASE_URL and self.CEREBRAS_API_KEY):
            return False
        return all(models)

    def graph_configured(self) -> bool:
        """Can this process actually reach Graph?

        Separate from `microsoft_configured` because they drifted apart in
        production: the web service held the client credentials but not the
        Graph URL, so sign-in worked perfectly while every Graph call 500ed.
        Anything that talks to Graph asks this first.
        """
        return bool(self.GRAPH_BASE_URL)

    def wa_gateway_configured(self) -> bool:
        """Same question, asked of the Baileys gateway (see WA_GATEWAY_URL)."""
        return bool(self.WA_GATEWAY_URL and self.WA_GATEWAY_SHARED_SECRET)

    def google_configured(self) -> bool:
        """Identity only — Google users have no mailbox to ingest."""
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_API_BASE_URL)

    def whatsapp_configured(self) -> bool:
        return bool(
            self.WHATSAPP_ACCESS_TOKEN
            and self.WHATSAPP_PHONE_NUMBER_ID
            and self.WHATSAPP_API_BASE_URL
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
