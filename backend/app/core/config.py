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
    # How long to keep waiting for an in-flight token refresh after the
    # primary timeout. Entra can answer a moment later with a rotated token,
    # and abandoning the thread then burns the stored token — the next refresh
    # answers `invalid_grant` and the mailbox is dead until a manual reconnect
    # (see `ms_auth._acquire_refresh_token`). The grace window is what keeps a
    # merely slow Entra from becoming a forced reconnect.
    TOKEN_REFRESH_GRACE_SECONDS: float = Field(default=15.0, gt=0)
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

    # --- Image decoding safety ---
    # Not a product limit — a bound on this process. Pillow only raises
    # DecompressionBombError above its own ~179 Mpx threshold, so a tiny
    # crafted file declaring, say, 120 Mpx opens with nothing worse than a
    # warning and then allocates hundreds of megabytes on decode, which is
    # enough to OOM-kill a small container. Every decode path checks the
    # declared size against this before touching pixels. Shared by avatars and
    # logos because it describes the machine, not the feature: far above any
    # real photo or wordmark, far below what hurts.
    IMAGE_DECODE_MAX_PIXELS: int = Field(default=30_000_000, gt=0)

    # --- Candidate avatar photos (shown only inside the candidate modal) ---
    AVATAR_MAX_UPLOAD_BYTES: int = Field(default=5 * 1024 * 1024, gt=0)
    # The photo is drawn at 56 CSS pixels. 512 is already 4x that — enough
    # headroom for a 3x display and for a future larger crop — and every pixel
    # beyond it is bytes the recruiter waits for and never sees. It was 1024,
    # which is 4x the area for no visible difference.
    AVATAR_MAX_PIXEL_DIMENSION: int = Field(default=512, gt=0)
    AVATAR_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
    # What every avatar is re-encoded *to*. Whatever the client uploads is
    # decoded and written back out in this format, which is what strips EXIF
    # (phone photos carry GPS). A Pillow format name, not a MIME type: the MIME
    # type is looked up from Pillow's own registry so the two can never drift.
    #
    # WEBP rather than PNG. PNG is lossless, and losslessly encoding a
    # photograph is the worst case for it — a phone snapshot lands at a few
    # megabytes where the same image in WEBP is tens of kilobytes, for a circle
    # 56 pixels across. WEBP keeps the alpha channel PNG was chosen for, and is
    # supported by every browser this product targets. Anything Pillow can save
    # is accepted here; only the default moved.
    AVATAR_STORED_FORMAT: str = "WEBP"
    # How long a browser may reuse avatar bytes it has already downloaded,
    # signed into the presigned URL as `response-cache-control` and stored on
    # the object. Tied to the URL's own lifetime by default rather than set
    # independently: caching bytes for longer than the URL that names them
    # cannot help, because the next URL is a different one.
    AVATAR_CACHE_MAX_AGE_SECONDS: int = Field(default=300, ge=0)

    # --- Client company logos (shown in the clients panel and sourcing) ---
    # A company logo, not a passport photo: its own limits, because the two
    # have no reason to move together and a shared constant would only reveal
    # the coupling when changing one broke the other.
    CLIENT_LOGO_MAX_UPLOAD_BYTES: int = Field(default=5 * 1024 * 1024, gt=0)
    # Same reasoning as the avatar bound: the mark is drawn at 56 pixels, and a
    # 1024 square PNG is four times the area for nothing anyone can see. A
    # wordmark stays legible far below that.
    CLIENT_LOGO_MAX_PIXEL_DIMENSION: int = Field(default=512, gt=0)
    CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
    # The logo counterpart of AVATAR_CACHE_MAX_AGE_SECONDS. Separate for the
    # same reason the two size limits are separate: the pages differ, and a
    # shared constant would only reveal the coupling by breaking one of them.
    CLIENT_LOGO_CACHE_MAX_AGE_SECONDS: int = Field(default=300, ge=0)

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
    # How many times a worker may pick up one CV document. `CandidateDocument.
    # attempts` is spent at claim time, mirroring the import and sourcing runs,
    # so a document whose parse times out or crashes is re-enqueued by
    # `rescan_stuck` a bounded number of times and then parked in `failed`
    # instead. Without a ceiling the pair loops forever — each loop a fresh
    # `parse_candidate_cv` job, each job up to several billed model calls — as
    # happened 2026-08-13 when slow DeepSeek responses made every CV in a batch
    # time out at the 300s arq budget and get re-enqueued every sweep.
    CV_PARSE_MAX_ATTEMPTS: int = Field(default=3, gt=0)
    # The ingest front half: read text, one identity model call, a match query.
    # Bounded by the same FlateDecode risk as the parse, plus a single model
    # call, so it shares the parse's ceiling rather than carrying one of its
    # own. A timed-out job leaves the row at `ingesting`, which `rescan_stuck`
    # routes back to `ingest_candidate_cv`.
    CV_INGEST_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0)
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
    # --- OCR fallback for scanned CVs ---
    # Off by default: a deployment that has not installed the Tesseract/
    # Ghostscript/QPDF toolchain (the Dockerfile layer) gets the current
    # `unreadable` behavior byte-for-byte. On, the empty-text branch in
    # `parse_candidate_cv` runs Tesseract via `app.services.cv.ocr` and flows
    # the recovered text through the same parse. `ocr_configured()` ANDs this
    # with a binary probe so a flag set without the toolchain degrades to the
    # same `unreadable` path with a named cause rather than a crash.
    CV_OCR_ENABLED: bool = False
    # Wall clock one OCR run may take. Well under the parse job's own timeout,
    # because OCR runs inside it: a scanned CV that takes the full parse budget
    # on OCR alone leaves nothing for the model call that follows.
    CV_OCR_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    # A ceiling on the pages OCR will touch. A genuine CV is a handful of pages;
    # a scanned document longer than this is almost certainly not a CV, and the
    # bound is what stops a hostile PDF from running Tesseract for an hour.
    CV_OCR_MAX_PAGES: int = Field(default=10, gt=0)
    # Tesseract language codes, `+`-joined (`eng`, `eng+chi_sim+tam`). English
    # ships with the base `tesseract-ocr` package; further codes need the
    # matching `tesseract-ocr-<code>` in the image. Carried as one string so the
    # orchestrator receives it verbatim.
    CV_OCR_LANGUAGES: str = "eng"
    # --- Legacy document conversion (.doc → .docx) ---
    # On by default, because LibreOffice headless is installed in the Dockerfile
    # and a `.doc` refusal is a real-world breakage (agencies hold Word 97-2003
    # CVs). The gate is ANDed with a binary probe in `conversion_configured()`
    # so a deployment without LibreOffice degrades to the honest refusal with a
    # named cause rather than a crash.
    CV_CONVERT_ENABLED: bool = True
    # Wall clock one conversion may take. LibreOffice is a full office suite
    # doing a real document load, so it is slower than a sniff — but a CV is a
    # handful of pages, and this is the bound that stops a corrupt or hostile
    # file holding a worker slot indefinitely.
    CV_CONVERT_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)

    # --- Job-order documents (New job order upload) ---
    # The largest job-description file the API will accept, counted as the
    # bytes arrive rather than trusted from `Content-Length`. Same shape as a
    # CV: a real job description is a handful of pages.
    OPPORTUNITY_DOCUMENT_MAX_UPLOAD_BYTES: int = Field(default=10 * 1024 * 1024, gt=0)
    # How long a download link stays valid. Same reasoning as the CV's: a
    # signed URL is a capability, and one that outlives the session it was
    # minted under is a capability nobody revoked.
    OPPORTUNITY_DOCUMENT_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
    # The wall clock a single job-description extraction may occupy an arq
    # worker for, model call included. Bounded by the same FlateDecode risk as
    # the CV parse, so it shares that ceiling's reasoning.
    OPPORTUNITY_DOCUMENT_EXTRACT_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0)
    # How many times a worker may pick up one job-description document.
    # `OpportunityDocument.attempts` is spent at claim time, mirroring the CV
    # parse and the import runs, so a document whose extraction times out or
    # crashes is re-enqueued by `rescan_stuck` a bounded number of times and
    # then parked in `failed` instead. Without a ceiling the pair loops
    # forever — each loop a fresh `extract_opportunity_document` job, each job
    # up to several billed model calls.
    OPPORTUNITY_DOCUMENT_MAX_ATTEMPTS: int = Field(default=3, gt=0)

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
    # The model the Job Intelligence engine asks. Empty falls back to
    # `EXTRACTION_MODEL_FAST` at call time (see `job_intelligence.understand.model`),
    # so a deployment that names only the one model still runs the analysis.
    JOB_INTELLIGENCE_MODEL: str = ""
    # The model the Candidate Intelligence engine asks. Same fallback idiom as
    # `JOB_INTELLIGENCE_MODEL` (see `candidate_intelligence.history.model`): an
    # empty value defaults to the fast extraction model at call time. Listed in
    # `.env.example` alongside the Job Intelligence knobs.
    CANDIDATE_INTELLIGENCE_MODEL: str = ""
    # The output budget the Candidate Intelligence engine gives each call.
    # Separate from `EXTRACTION_MAX_TOKENS` because the work pass is the deepest
    # reasoning prompt in the system: `deepseek-v4-flash` counts reasoning tokens
    # against `max_tokens`, and the work decomposition burned the extraction
    # budget (16000) on reasoning alone, returning `reasoning` content with no
    # `content` — an empty response the job layer fails for good. 65536 is 4×
    # the budget that failed and well under the model's 384K output ceiling,
    # leaving room for a long trace and the large JSON answer (verified against
    # the live API, not theorised).
    CANDIDATE_INTELLIGENCE_MAX_TOKENS: int = Field(default=65536, gt=0)
    # The reasoning effort the Candidate Intelligence engine asks for. Matches
    # `EXTRACTION_REASONING_EFFORT_FAST`'s default; explicit because the right
    # answer is a property of the model, and the knob must not silently track
    # extraction's if the two ever diverge.
    CANDIDATE_INTELLIGENCE_REASONING_EFFORT: str = "low"
    # How many times the job re-asks after an empty response (`LLMNoContent`).
    # A no-content answer is not an answer — the model spent its budget thinking
    # and never emitted anything — so re-asking is a materially different
    # request, and the "temperature zero makes a retry the same answer twice"
    # rule applies only to real answers. One retry covers provider hiccups and
    # the tail of a budget miss; the 64K ceiling makes the retry overwhelmingly
    # likely to answer.
    CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRIES: int = Field(default=1, ge=0)
    # Pause between no-content retries. Small: it is backoff for a transient
    # provider state, not a rate limit — the job already spends an arq slot.
    CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRY_DELAY_SECONDS: float = Field(
        default=2.0, ge=0
    )

    # --- DeepSeek (the classifier and extraction) ---
    # The gate is the highest-volume call in the system — one per email, on
    # every email, forever — so it runs on its own provider rather than through
    # the router the extraction calls use. DeepSeek replaced DeepSeek as that
    # provider (the gate used to measure ~36ms round trip against
    # deepseek-v4-flash), because the gate sits between a fetched email and
    # everything else.
    #
    # Extraction joined it after the router path failed in production, not on
    # principle. The escalation model (§32) rejected our fourteen-field schema
    # outright — "the compiled grammar is too large" — and every real email sat
    # at `extracting` with nothing to show for it. The same document, sent as
    # prompt text with a plain `json_object` response format, is answered
    # correctly here in ~1.5s and at a fraction of the price.
    DEEPSEEK_BASE_URL: str = ""
    DEEPSEEK_API_KEY: str = ""

    # --- Embeddings (semantic candidate matching) ---
    # Routed through OpenRouter — the same provider the extraction and
    # classification calls already use — under the model id
    # `openai/text-embedding-3-small`. OpenRouter's embeddings endpoint is
    # OpenAI-compatible (same /embeddings path, same {model, input} body, same
    # data[].embedding response), so the embeddings client in
    # app/services/llm/embeddings.py needs no provider-specific code.
    #
    # `EMBEDDING_API_KEY` is optional: when empty the embeddings client falls
    # back to `OPENROUTER_API_KEY`, so a deployment that already has the
    # router key configured gets embeddings with no extra setup. Set
    # `EMBEDDING_API_KEY` only to isolate embeddings billing behind its own
    # key.
    #
    # Privacy parity: CV text already leaves the system for LLM explanations
    # (DeepSeek). Embeddings send the same text through the same router the
    # extraction calls already use, so no new data boundary is crossed.
    EMBEDDING_BASE_URL: str = "https://openrouter.ai/api/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "openai/text-embedding-3-small"
    EMBEDDING_DIM: int = 1536
    # Truncate CV text before embedding. Bounding the input bounds the cost and
    # keeps one very long CV from dominating a batch. 8000 chars is well past
    # the discriminating detail of a CV and well under the model's context.
    EMBEDDING_MAX_CHARS: int = Field(default=8000, gt=0)
    # How many texts one embeddings call carries. The provider charges per
    # call and per token, so batching is the saving; bounded because one bad
    # input costs every text in the batch a retry.
    EMBEDDING_BATCH_SIZE: int = Field(default=100, gt=0, le=2048)
    # The wall clock one embedding job may take. A job is one candidate's
    # assembled text in one provider call, so this is a ceiling on a transient
    # provider stall rather than a budget for real work.
    EMBEDDING_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
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
    # the gate's model returns a `reasoning` field and no `content` at all —
    # verified, not theorised — so this must leave room for both.
    CLASSIFIER_MAX_TOKENS: int = Field(default=4000, gt=0)
    # How often the supervisor sweeps for `fetched` rows to classify. This is
    # the latency an email waits before the gate sees it at all, now that
    # `fetch_email` no longer enqueues a per-email job: batching trades a
    # little delay for the per-call overhead paid once instead of once each.
    # The gate's model reasons before it answers, and at the default effort it
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
    # Deliberate re-extraction of emails whose latest extraction ran under an
    # older prompt version (see `replay_stale_extractions` in tasks.py). On its
    # own, slower clock than classification: every replayed email costs a model
    # call, so a prompt upgrade drains a backlog gradually rather than paying
    # for all of it at once. The claim resolver caps a single sweep too, and
    # `RESUME_JOB` maps a worker-killed `replaying` row back to `replay_email`,
    # so nothing is stranded and nothing is silently re-run as a plain
    # extraction.
    REPLAY_SWEEP_INTERVAL_SECONDS: float = Field(default=600.0, gt=0)
    REPLAY_SWEEP_LIMIT: int = Field(default=25, gt=0)
    # Generous next to GRAPH_TIMEOUT_SECONDS on purpose: a long recruitment
    # email on a strong model routinely spends a minute generating, and a
    # timeout here costs the whole extraction plus a retry's worth of tokens.
    LLM_TIMEOUT_SECONDS: float = 90.0
    # An extraction answers for fourteen fields per vacancy and quotes the email
    # for each, so its completions are an order of magnitude longer than the
    # gate's one-word verdicts. Too low and the response is truncated mid-JSON,
    # which arrives as `LLMInvalidJSON` and looks like a model problem.
    # Doubled from 16000 after production issue: DeepSeek v4 Flash counts
    # reasoning tokens against max_tokens, and the extraction prompt + email body
    # was large enough to exhaust the budget on reasoning alone, leaving zero
    # tokens for the JSON response — logged as LLMNoContent (arq logs 2026-08-11).
    EXTRACTION_MAX_TOKENS: int = Field(default=32000, gt=0)
    # Cap on the email source text sent to extraction, in characters of the
    # `to_text` output. A job order states everything in its first screen —
    # the Etiqa sample is ~800 chars — and a long reply chain can be 10K+
    # tokens of quoted history the model does not need. Truncating the tail
    # keeps the input proportional to the signal; the gate already truncates
    # with CLASSIFIER_CHARS_PER_EMAIL for the same reason.
    EXTRACTION_MAX_CHARS: int = Field(default=4000, gt=0)
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
    # v2: the model-facing schema gained `salary_min`/`salary_max` so compound
    # offers ("$4500 basic + $800 allowance") store a usable range instead of
    # being refused by the deterministic parser.
    PROMPT_VERSION: str = "v2"
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
    # Symbol-to-code map. The bare "$" is mapped to SGD because this
    # deployment serves Singapore recruitment exclusively — the mail that says
    # "$5,500" means the same dollars the recruiter will place the candidate
    # for, and leaving it unnamed is what made a stated band abstain from the
    # score. "S$" and "$" both map to SGD so the explicit and the bare form
    # agree (longest match wins, so "S$" is tried first). A deployment serving
    # a market where "$" is ambiguous should set its own map.
    SALARY_CURRENCY_SYMBOLS: str = "S$=SGD,$=SGD,RM=MYR,€=EUR,£=GBP,¥=JPY,₹=INR"
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
    BUDDIES_PAGE_LIMIT: int = Field(default=200, gt=0)

    # `?eligible_for=` (app/api/candidates.py) cannot filter in SQL — a
    # candidate is only known `not_met` after `eligibility.evaluate()` runs in
    # Python, and re-expressing MOM's rules as SQL predicates would put them
    # in two places. So the endpoint loads up to this many of the tenant's
    # matching candidates, evaluates each, and pages the *filtered* result in
    # memory. This is the ceiling on that scan, not on the page returned —
    # an agency of a hundred recruiters with a genuinely large active list
    # still fits comfortably under 5,000, and the number is small enough that
    # eco-nano never builds anywhere near that many ORM objects on one
    # request. Past this ceiling the endpoint reports `scan_truncated: true`
    # and `scanned: <n>` rather than silently answering from a partial view —
    # a short list must never look like completeness.
    CANDIDATES_ELIGIBILITY_SCAN_LIMIT: int = Field(default=5_000, gt=0)

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

    # --- MDW Work Permit eligibility (app/services/sourcing/eligibility.py) ---
    # MOM policy, not a fact about this code — it changes on the regulator's
    # schedule, not on a release cycle, so it lives here rather than as a
    # literal in the eligibility module. `eligibility.py` stays pure (no
    # settings import) and takes these as arguments; only the caller reads
    # them from here.
    MDW_MIN_AGE_YEARS: int = Field(default=23, gt=0)
    # Exclusive upper bound — "under 50" in MOM's own wording, so a candidate
    # turns ineligible on their 50th birthday, not the day after.
    MDW_MAX_AGE_YEARS_EXCLUSIVE: int = Field(default=50, gt=0)
    MDW_MIN_EDUCATION_YEARS: int = Field(default=8, ge=0)
    # ISO 3166-1 alpha-2, comma-separated, matching `Candidate.nationality`'s
    # own encoding so the two never need translating against each other.
    # Comma-separated string rather than a list because that is how every
    # other multi-value setting in this file is expressed — see
    # `FREE_EMAIL_DOMAINS_RAW` — and parsed the same way, once, via the
    # property below.
    MDW_APPROVED_SOURCE_COUNTRIES_RAW: str = Field(
        default="BD,KH,HK,IN,ID,MO,MY,MM,PH,KR,LK,TW,TH",
        alias="MDW_APPROVED_SOURCE_COUNTRIES",
    )

    @property
    def MDW_APPROVED_SOURCE_COUNTRIES(self) -> frozenset[str]:
        return frozenset(
            part.strip().upper()
            for part in self.MDW_APPROVED_SOURCE_COUNTRIES_RAW.split(",")
            if part.strip()
        )

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

    # --- Client auto-discovery (spec 2026-08-02) ---
    # A header-only scan of the signed-in recruiter's own mailbox. Every knob
    # lives here rather than in the service because each one is a deployment
    # judgement — how far back a "current" client relationship reaches, what a
    # page of Graph headers costs, which senders are machines — not a fact
    # about the code.
    CLIENT_DISCOVERY_LOOKBACK_DAYS: int = Field(default=90, gt=0)
    # `$top` per Graph page. 100 is universally accepted on the messages
    # endpoints; raising it is a deployment experiment, which is why it is a
    # setting and not a literal.
    CLIENT_DISCOVERY_PAGE_SIZE: int = Field(default=100, gt=0, le=1000)
    # Headers the whole scan (inbox + sent together) may read before stopping.
    # The cap is consulted per whole page, like the delta walk, so it can
    # overshoot by at most one page rather than silently skip mid-page.
    CLIENT_DISCOVERY_MAX_MESSAGES: int = Field(default=10_000, gt=0)
    # How many ranked new domains one run stores and shows. Past this the run
    # says `domains_truncated` rather than pretending the list was complete.
    CLIENT_DISCOVERY_MAX_DOMAINS: int = Field(default=200, gt=0)
    # How many contacts discovery will add to any one client in one
    # application — a bound on additions, not on the client's contact list.
    CLIENT_DISCOVERY_MAX_CONTACTS_PER_CLIENT: int = Field(default=10, gt=0)
    # Relationship score weights (source plan, "Relationship Score"). Only the
    # ratios matter; they are settings because which signal an agency trusts —
    # mail received, mail sent, breadth of contacts — is theirs to tune.
    CLIENT_DISCOVERY_WEIGHT_RECEIVED: float = Field(default=1.0, ge=0)
    CLIENT_DISCOVERY_WEIGHT_SENT: float = Field(default=2.0, ge=0)
    CLIENT_DISCOVERY_WEIGHT_UNIQUE_CONTACTS: float = Field(default=5.0, ge=0)
    # The recent-activity bonus and the window that earns it.
    CLIENT_DISCOVERY_RECENCY_BONUS: float = Field(default=10.0, ge=0)
    CLIENT_DISCOVERY_RECENCY_DAYS: int = Field(default=14, gt=0)
    # A run left `running` longer than this was abandoned by a dead worker.
    # The supervisor sweep (`sweep_stale_client_discovery_runs` in
    # `app/workers/tasks.py`) parks such rows in `failed`; the scan POST keeps
    # its own stale check so a sweep-free deployment (or a sweep that has not
    # ticked yet) still never blocks a recruiter's scan forever.
    CLIENT_DISCOVERY_STALE_RUNNING_MINUTES: int = Field(default=15, gt=0)
    # A run left `pending` longer than this never was claimed: the enqueue was
    # lost after the row committed, or the queue consumer died before taking
    # the job. The sweep fails it — arq's retry only resumes rows a job
    # actually claimed, so nothing else would ever move it.
    CLIENT_DISCOVERY_STALE_PENDING_MINUTES: int = Field(default=10, gt=0)
    # How often the supervisor checks for stale discovery runs. Independent of
    # the run's own staleness bounds: a stale row can sit for up to this long
    # before the sweep notices it, and the scan POST's own stale check covers
    # the gap for a recruiter who clicks during it.
    CLIENT_DISCOVERY_SWEEP_INTERVAL_SECONDS: float = Field(default=300.0, gt=0)
    # The wall clock one scan may occupy an arq worker for. Headers are cheap
    # but a large mailbox is many pages, and a job this cuts short is simply a
    # failed run the user re-starts with one click.
    CLIENT_DISCOVERY_JOB_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)
    # Local parts that identify machinery rather than a person. An entry
    # matches the lowercased local part (with any `+tag` stripped) exactly, or
    # as a prefix whose next character is not a letter — so `noreply1` and
    # `newsletter-team` match while a surname like `alertan` does not.
    CLIENT_DISCOVERY_SYSTEM_LOCALPARTS_RAW: str = Field(
        default="noreply,no-reply,donotreply,do-not-reply,notification,"
        "notifications,mailer-daemon,postmaster,bounce,bounces,newsletter,"
        "newsletters,alert,alerts,updates,digest",
        alias="CLIENT_DISCOVERY_SYSTEM_LOCALPARTS",
    )

    @property
    def CLIENT_DISCOVERY_SYSTEM_LOCALPARTS(self) -> frozenset[str]:
        return frozenset(
            part.strip().lower()
            for part in self.CLIENT_DISCOVERY_SYSTEM_LOCALPARTS_RAW.split(",")
            if part.strip()
        )

    # Domains that are mail *about* recruitment rather than mail *from* a
    # client — job boards and bulk-mail infrastructure. Suffix-matched, so
    # `bounce.linkedin.com` is covered by `linkedin.com`. Deliberately minimal:
    # a noisy row costs the user an unticked checkbox, while an over-broad
    # exclusion hides a real client with nothing anywhere to say so.
    CLIENT_DISCOVERY_EXCLUDED_DOMAINS_RAW: str = Field(
        default="linkedin.com,indeed.com,glassdoor.com,jobstreet.com,"
        "jobsdb.com,mycareersfuture.gov.sg,efinancialcareers.com,"
        "facebookmail.com,amazonses.com,sendgrid.net,mailchimp.com,"
        "mailgun.org,mandrillapp.com,sparkpostmail.com",
        alias="CLIENT_DISCOVERY_EXCLUDED_DOMAINS",
    )

    @property
    def CLIENT_DISCOVERY_EXCLUDED_DOMAINS(self) -> frozenset[str]:
        return frozenset(
            part.strip().lower()
            for part in self.CLIENT_DISCOVERY_EXCLUDED_DOMAINS_RAW.split(",")
            if part.strip()
        )

    # Deterministic noise markers for the pre-gate rule filter (plan Task 4;
    # `gate_rules`). Comma-separated, case-insensitive substrings. A subject
    # matching any marker, or a sender local-part matching any marker, is
    # answered `non_recruitment` without an LLM call. The defaults are the
    # well-known shapes of inbox noise; a tenant whose recruiters see a
    # particular newsletter or portal name can add it here. Empty means "use
    # the built-in defaults", so a deployment never has to enumerate them.
    NOISE_SUBJECT_MARKERS: str = Field(
        default="",
        description="Comma-separated subject substrings that mark a non-job-order",
    )
    NOISE_LOCALPART_MARKERS: str = Field(
        default="",
        description="Comma-separated sender local-part substrings that mark noise",
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
    # The interactive analysis queue. User-initiated analyses — Job
    # Intelligence (`run_job_intelligence`) and Candidate Intelligence
    # (`run_candidate_intelligence`) — land on their own queue, consumed by a
    # dedicated arq worker with its own `max_jobs` budget. A background replay
    # or extraction backlog on the default queue can therefore never starve a
    # recruiter's click: the interactive worker always has free slots for it.
    # Queue names are the full Redis zset keys arq uses.
    ARQ_INTERACTIVE_QUEUE: str = "arq:interactive"
    # The interactive worker's own concurrency ceiling, separate from
    # `ARQ_MAX_JOBS` so analysis capacity never has to compete with ingestion.
    ARQ_INTERACTIVE_MAX_JOBS: int = Field(default=5, gt=0)

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
    # Floor between two sends on the same session (plan §9, P5), with jitter
    # so a burst does not become a metronome. Enforced here — not only in
    # `gateway/` — because the pre-dispatch transaction that closes the daily
    # cap race (see `candidate_whatsapp.py#_claim_send`) is the natural place
    # to also serialise spacing: both read the same locked `wa_sessions` row.
    # Default matches `WA_SEND_MIN_INTERVAL_SECONDS` in `.env.example`'s
    # gateway block, which the gateway itself also enforces as a second,
    # independent line of defence against a caller that bypasses this API.
    WA_SEND_MIN_INTERVAL_SECONDS: int = 30
    # Liveness sweep (P5, plan §6): a `pending` row older than this was left
    # by a process that died mid-send — nobody will ever learn the outcome,
    # so it becomes `unknown`, never `failed` (§15: we never observed a
    # refusal). Comfortably above `WA_GATEWAY_TIMEOUT_SECONDS` (5s default) so
    # a send merely slow, not dead, is never declared unknown mid-flight.
    WA_SEND_STALE_PENDING_MINUTES: int = Field(default=10, gt=0)
    # How often the supervisor sweeps for stale `pending` WA gateway sends.
    # Independent of NOTIFY_SWEEP_INTERVAL_SECONDS on purpose — a different
    # concern with a different tolerable latency.
    WA_SWEEP_INTERVAL_SECONDS: float = Field(default=120.0, gt=0)
    # Session liveness sweep (plan §6, the background half —
    # `20260729_2400_wa_liveness_sweep.py`). A `connected`/`reconnecting`
    # session whose `last_liveness_check_at` is older than this (or NULL) is
    # due: the sweep asks the gateway's status endpoint, which is enough to
    # bring the database up to date because the gateway already pushes
    # `POST /api/wa/internal/status` on every change — asking does not need
    # to write `status` itself (§6's single-writer invariant). Independent of
    # WA_SWEEP_INTERVAL_SECONDS: that sweep resolves stale sends, this one
    # resolves stale knowledge about a session's socket.
    WA_LIVENESS_CHECK_STALE_MINUTES: int = Field(default=15, gt=0)
    # How often the supervisor runs the liveness sweep.
    WA_LIVENESS_SWEEP_INTERVAL_SECONDS: float = Field(default=180.0, gt=0)
    # Bound on one sweep call — same reasoning as WA_SWEEP_LIMIT-equivalent
    # constants elsewhere: caps how long one call holds FOR UPDATE SKIP
    # LOCKED. The SQL function itself also caps at 500.
    WA_LIVENESS_SWEEP_LIMIT: int = Field(default=200, gt=0)
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
    # One approved template covers all six candidate events: they carry the
    # same four parameters (what happened, who, by whom, note), and six
    # near-identical templates would be six things to get approved and six
    # things to drift.
    WHATSAPP_TEMPLATE_CANDIDATE_UPDATE: str = ""
    WHATSAPP_TEMPLATE_LANG: str = "en"

    # A forty-vacancy morning is forty billable WhatsApp messages otherwise.
    NOTIFY_RATE_CAP_PER_HOUR: int = Field(default=6, gt=0)
    NOTIFY_LINK_TOKEN_TTL_MINUTES: int = Field(default=15, gt=0)
    NOTIFY_MAX_ATTEMPTS: int = Field(default=5, gt=0)
    NOTIFY_MAX_FAILURES: int = Field(default=3, gt=0)
    # The bound on backpressure re-queues, which deliberately do NOT consume
    # NOTIFY_MAX_ATTEMPTS (see SendResult.backpressure). A wall clock rather
    # than a second attempt count, because the thing that actually stops being
    # worth doing is sending *late* news: a count would mean whatever the
    # provider's current spacing happens to make it mean — five refusals is
    # two minutes at a 30-second floor and half an hour at a five-minute one —
    # so the same number would silently change policy every time the gateway
    # was tuned. Thirty minutes drains roughly sixty spaced sends, far more
    # than one recruiter's evening, while a job order that surfaces half an
    # hour late is still news.
    NOTIFY_BACKPRESSURE_DEADLINE_MINUTES: int = Field(default=30, gt=0)
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
    # CV-to-job-order semantic similarity. Sits between salary and employer
    # because it is the signal that recovers candidates the structured fields
    # miss — a "React" CV against a "ReactJS" vacancy — which is the whole
    # reason the embedding layer exists.
    SOURCING_WEIGHT_SEMANTIC: float = Field(default=2.0, ge=0)
    SOURCING_WEIGHT_EMPLOYER: float = Field(default=1.0, ge=0)
    SOURCING_WEIGHT_SALARY: float = Field(default=2.0, ge=0)
    SOURCING_WEIGHT_TENURE: float = Field(default=1.0, ge=0)
    SOURCING_WEIGHT_RECENCY: float = Field(default=1.0, ge=0)

    # --- Semantic retrieval (the recall half of hybrid matching) ---
    # How many nearest neighbours the ANN query surfaces for the rescue path.
    # These are candidates the structured scorer may have dropped entirely
    # (no title, no skills on record) whose CV nonetheless matches the job
    # order. Bounded because the rescue re-scores each one, and a run that
    # re-scores a thousand rows has stopped being a shortlist.
    SOURCING_SEMANTIC_RECALL_K: int = Field(default=50, gt=0)
    # The minimum cosine similarity at which a rescued candidate is kept.
    # Below this the CV is too far from the job order to be worth showing on
    # similarity alone; the candidate keeps nothing they did not earn from the
    # structured components.
    SOURCING_SEMANTIC_FLOOR: float = Field(default=0.35, ge=0, le=1)

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
    # How many job orders "Find Job" shortlists for a candidate. The candidate
    # modal's reverse-direction matcher scores every current revision the
    # recruiter can see and keeps this many of the best — the same shape the
    # sourcing run's `SOURCING_MAX_MATCHES` gives the forward direction, with
    # the cap small because the result is shown in a modal rather than paged.
    CANDIDATE_JOBS_TOP_N: int = Field(default=5, gt=0)

    # --- Job Intelligence ---
    # The wall clock one analysis may take: three DeepSeek calls (understand →
    # persona → search) in sequence. Generous because a reasoning-heavy JD can
    # make each call slow, and a job this cuts short is left at `running` for
    # `rescan_stuck` to re-enqueue. Three calls × the per-call LLM timeout is
    # the natural ceiling; 600s matches the sourcing job, which is also LLM
    # work bounded by model latency rather than data size.
    JOB_INTEL_JOB_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)
    # How many times a worker may pick one analysis up before giving up on it.
    # Same reasoning as `SOURCING_MAX_ATTEMPTS`: a crashed worker deserves a
    # retry, a job order that crashes the pipeline every time does not.
    JOB_INTELLIGENCE_MAX_ATTEMPTS: int = Field(default=3, gt=0)
    # The Candidate Intelligence analysis: three DeepSeek calls (career →
    # capability → profile) in the worker, the same shape Job Intelligence
    # takes. Same timeout ceiling (LLM work bounded by model latency) and the
    # same attempt cap (a candidate that crashes the pipeline every time
    # reaches `failed` rather than looping forever).
    CANDIDATE_INTELLIGENCE_JOB_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)
    CANDIDATE_INTELLIGENCE_MAX_ATTEMPTS: int = Field(default=3, gt=0)

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

    def deepseek_configured(self, *models: str) -> bool:
        """The same question as `llm_configured`, asked of the gate's provider.

        Kept separate rather than adding a flag to `llm_configured`: the two
        answer for different credentials, and a single function returning True
        because the *other* provider was configured is exactly how the gate
        would end up classifying real mail against a hostless URL.
        """
        if not (self.DEEPSEEK_BASE_URL and self.DEEPSEEK_API_KEY):
            return False
        return all(models)

    def embedding_configured(self) -> bool:
        """Can this process reach the embeddings provider?

        Defaults to the OpenRouter key — the same one extraction and
        classification already use — so a deployment that has the router
        configured gets embeddings with no extra setup. `EMBEDDING_API_KEY`
        overrides when embeddings should bill under their own key.

        Kept as its own check (rather than folded into `llm_configured`)
        because embeddings are a different endpoint with a different failure
        mode, and a sourcing run that falls back to the six-component scorer
        when embeddings are absent is the correct, graceful degradation — not
        a failure to detect.
        """
        key = self.EMBEDDING_API_KEY or self.OPENROUTER_API_KEY
        return bool(self.EMBEDDING_BASE_URL and key)

    def embedding_api_key(self) -> str:
        """The key the embeddings client should send, resolved once.

        `EMBEDDING_API_KEY` wins when set; otherwise the OpenRouter key is
        reused. Centralised here so the client and this check agree on which
        key is in play — a divergence between them is exactly how a run would
        pass the configured gate and then fail the request.
        """
        return self.EMBEDDING_API_KEY or self.OPENROUTER_API_KEY

    def ocr_configured(self) -> bool:
        """Is the scanned-PDF OCR fallback actually runnable here?

        ANDs the flag with a binary probe so a flag set without the toolchain
        degrades to the same `unreadable` path with a named cause rather than a
        crash inside `ocrmypdf`. The probe (`ocr_available`) is imported lazily
        so this module — which loads early — does not pull the OCR toolchain's
        Python deps at config import time.
        """
        if not self.CV_OCR_ENABLED:
            return False
        from app.services.cv.ocr import ocr_available

        return ocr_available()

    def conversion_configured(self) -> bool:
        """Can this process convert legacy Office documents (.doc) to .docx?

        ANDs the flag with a binary probe (`converter_available`) so a flag set
        without LibreOffice degrades to the honest refusal with a named cause
        rather than a crash inside `soffice`. On by default because the
        Dockerfile installs LibreOffice and a `.doc` refusal is a real-world
        breakage; flip `CV_CONVERT_ENABLED=false` to disable.
        """
        if not self.CV_CONVERT_ENABLED:
            return False
        from app.services.cv.convert import converter_available

        return converter_available()

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
