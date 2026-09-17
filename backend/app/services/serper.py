"""Web-search evidence retrieval for the Candidate Identity Resolver.

Spec: `serper design.md` §10 (provider abstraction), §11 (request model), §12
(normalization), §39 (retry), §42 (security), §43 (logging).

Serper is used as an **evidence retrieval layer**, not a candidate database
and not a contact provider (§2). Nothing above this module talks to Serper's
request/response shape: `WebSearchProvider` and the normalized
`NormalizedSearchResult` are the seam, so a future provider (SerpApi, Bing,
Brave, internal search) is a new class and no resolver change (§10, §41).

Two properties are load-bearing:

1. **The API key never leaves this process** (§42). It is sent as the
   `X-API-KEY` header and is never placed in an exception message, a return
   value, or a log record. Every log line carries the query *hash*, never the
   query wording, and never the key (§43).
2. **Transient failures retry; permanent ones do not** (§39). A timeout, a
   429, a 5xx or a network error is retried with 500 ms then 1500 ms and
   jitter; a 4xx client error is raised immediately — retrying a malformed
   request only spends quota to fail again.
"""

from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import anyio
import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# §39: attempt 1 → 500 ms, attempt 2 → 1500 ms, attempt 3 → fail. Jitter is
# added by the caller so retries from many candidates do not synchronise.
_RETRY_DELAYS_MS = (500, 1500)
# Fraction of the base delay added as jitter (0–33%), matching the WA gateway's
# habit of not stacking retries on a sharp boundary.
_JITTER = 0.33
_TRANSIENT_STATUSES = {429}
# SSRF-shaped guard: Serper answers 5xx when it is overwhelmed, which is
# transient. Everything below is the caller's fault and is raised at once.
_CLIENT_ERROR_FLOOR = 400
_SERVER_ERROR_FLOOR = 500


class SerperUnconfiguredError(Exception):
    """No API key configured — the feature is off, not broken.

    Carries no key material: there is none to carry. The route maps this to a
    structured `unconfigured` status rather than a 500, the same reading
    `career_bot_configured()` takes.
    """


class SerperError(Exception):
    """A search failed after retries or on a permanent client error.

    `status` is the HTTP status when the provider answered with one, else
    None. The message never contains the key or the Authorization/X-API-KEY
    header value; it may name the provider and the status, which are safe.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class SearchQuery:
    """The logical request (§11). Provider-specific parameters are mapped
    inside `SerperSearchProvider.search`, never above this line."""

    query: str
    country: str | None = None
    language: str | None = None
    result_limit: int | None = None


@dataclass(frozen=True)
class NormalizedSearchResult:
    """One result, in our shape (§12). Resolution logic operates on this and
    never on a raw Serper response."""

    title: str
    url: str
    snippet: str | None
    domain: str
    position: int | None
    provider: str
    query: str
    retrieved_at: str

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": self.domain,
            "position": self.position,
            "provider": self.provider,
            "query": self.query,
            "retrievedAt": self.retrieved_at,
        }


@dataclass
class SearchResultSet:
    """Every result for one query, plus the query that produced it."""

    query: str
    results: list[NormalizedSearchResult] = field(default_factory=list)


class WebSearchProvider(Protocol):
    """The seam (§10). One method today; `health()`/`estimate_cost()` from §41
    are deliberately not implemented until a caller needs them."""

    async def search(self, query: SearchQuery) -> SearchResultSet: ...


def _domain(url: str) -> str:
    """The bare hostname, lowercased, `www.` stripped — the grouping key for
    entity clustering (§18). An unparseable URL yields an empty domain rather
    than raising: a stray result is not worth failing a whole search."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalize(
    raw: dict, query: str, retrieved_at: str
) -> NormalizedSearchResult:
    url = str(raw.get("link") or "").strip()
    snippet = raw.get("snippet")
    return NormalizedSearchResult(
        title=str(raw.get("title") or "").strip(),
        url=url,
        snippet=str(snippet).strip() if isinstance(snippet, str) and snippet else None,
        domain=_domain(url),
        position=raw.get("position") if isinstance(raw.get("position"), int) else None,
        provider="serper",
        query=query,
        retrieved_at=retrieved_at,
    )


class SerperSearchProvider:
    """Concrete `WebSearchProvider` backed by Serper's search endpoint (§10).

    One client per call, closed in `finally` — the `app/services/wa_gateway.py`
    idiom. `httpx.HTTPError` is caught rather than propagated so a dead
    provider surfaces as `SerperError` (retryable) rather than an httpx
    exception a caller has to know about.
    """

    PROVIDER = "serper"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else settings.SERPER_API_KEY
        self._base_url = base_url or settings.SERPER_BASE_URL

    async def search(self, query: SearchQuery) -> SearchResultSet:
        if not self._api_key:
            raise SerperUnconfiguredError("Serper is not configured.")

        body: dict = {
            "q": query.query,
            "gl": query.country or settings.IDENTITY_COUNTRY,
            "hl": query.language or settings.IDENTITY_LANGUAGE,
            "num": query.result_limit or settings.IDENTITY_RESULT_LIMIT,
        }
        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        attempts = len(_RETRY_DELAYS_MS) + 1
        for attempt in range(attempts):
            try:
                payload = await self._post(body, headers)
            except SerperError as exc:
                # Permanent client error — do not retry (§39).
                if exc.status is not None and _CLIENT_ERROR_FLOOR <= exc.status < _SERVER_ERROR_FLOOR:
                    raise
                last_error = exc
            except Exception as exc:  # network/timeout — transient
                last_error = exc
            else:
                results = [
                    _normalize(item, query.query, _now_iso())
                    for item in payload.get("organic", [])
                    if isinstance(item, dict)
                ]
                log.info(
                    "serper_query_ok",
                    provider=self.PROVIDER,
                    query_hash=_query_hash(query.query),
                    result_count=len(results),
                    attempt=attempt + 1,
                )
                return SearchResultSet(query=query.query, results=results)

            if attempt < len(_RETRY_DELAYS_MS):
                await _sleep_with_jitter(_RETRY_DELAYS_MS[attempt])

        status = getattr(last_error, "status", None)
        log.warning(
            "serper_query_failed",
            provider=self.PROVIDER,
            query_hash=_query_hash(query.query),
            status=status,
            attempts=attempts,
        )
        raise SerperError("The web search provider could not be reached.", status=status)

    async def _post(self, body: dict, headers: dict) -> dict:
        """One HTTP round trip. Raises `SerperError` (with `status`) for a
        non-2xx answer so `search` can decide retryable vs permanent."""
        try:
            async with httpx.AsyncClient(timeout=settings.SERPER_TIMEOUT_SECONDS) as client:
                res = await client.post(self._base_url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            # Message deliberately excludes the URL and headers so the key
            # cannot leak through an exception string.
            raise SerperError("The web search provider could not be reached.") from exc
        if res.status_code >= _SERVER_ERROR_FLOOR or res.status_code in _TRANSIENT_STATUSES:
            raise SerperError(
                "The web search provider returned a temporary error.",
                status=res.status_code,
            )
        if res.status_code >= _CLIENT_ERROR_FLOOR:
            raise SerperError(
                "The web search provider rejected the request.",
                status=res.status_code,
            )
        try:
            return res.json()
        except ValueError as exc:
            raise SerperError("The web search provider returned malformed data.") from exc


async def _sleep_with_jitter(base_ms: int) -> None:
    import random

    delay = base_ms * (1 + random.random() * _JITTER) / 1000
    await anyio.sleep(delay)


def _now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def _query_hash(query: str) -> str:
    """A short, non-reversible fingerprint of the query (§43). The query
    wording itself is never logged — a query built from a candidate's name and
    employer is personal data, and §42 forbids it reaching a log."""
    import hashlib

    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
