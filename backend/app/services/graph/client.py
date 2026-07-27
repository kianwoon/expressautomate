"""Thin Microsoft Graph HTTP client (plan §7, §9).

Deliberately thin. It owns three things: authentication, the immutable-ID
preference, and classifying Graph's responses into "gone", "come back later",
and "this is your problem". Everything else — retries, backoff, concurrency
limits — belongs to the job layer, which is the only place that knows whether a
retry is worth paying for and how long a caller may block.

The classification is the point. `fetch_email` has to tell a message that no
longer exists (record it, never retry) from a throttle (retry, after the delay
Graph named) from a missing scope (retrying forever would bury the real cause).
Callers branch on exception type, not status codes.
"""

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


# Every token this application holds is a *delegated* token belonging to the
# mailbox's own owner, so `/me` and `/users/{that owner}` name the same mailbox.
# They are not equally supported, though: Graph rejects `/users/{id}` for
# personal Microsoft accounts, which is most of the intended user base here.
# That failure cost a production afternoon — Graph answered 503, so it read as
# an outage and retried forever instead of naming the unsupported path.
#
# `/me` works for both account types, and it takes the identity from the token
# rather than from a string we carry alongside it — one fewer way for the two
# to disagree. `ms_user_id` stays on the row: it is what the webhook matches a
# notification against, and what a future shared-mailbox path would need.
MAILBOX_ROOT = "/me"


def warn_if_unconfigured(process: str) -> bool:
    """Say once, at startup, that this process cannot reach Graph.

    Every process that calls Graph says it, because the failure is otherwise
    unattributable: an empty base URL makes httpx build a hostless URL and
    raise deep in its transport, far from anything naming the setting. That is
    how it reached production — set on the workers, missing on the web
    service, silent until the first web-service Graph call months later.

    Not fatal. Sign-in, the site and the webhook all work without it, and
    refusing to boot would turn one broken feature into an outage.
    """
    if settings.graph_configured():
        return True
    log.error(
        "graph_base_url_missing",
        process=process,
        detail="Mailbox reads will fail until GRAPH_BASE_URL is set.",
    )
    return False

# Graph message ids change when a message moves between folders. Every dedup
# constraint in the schema is keyed on that id, so asking for the immutable
# form on every request is what keeps a filed email from re-ingesting as new.
IMMUTABLE_ID_HEADER = 'IdType="ImmutableId"'


class GraphError(Exception):
    """A Graph response that is not modelled more precisely."""


class GraphNotFound(GraphError):
    """The resource is gone. Never worth retrying."""


class GraphResyncRequired(GraphError):
    """A delta token is no longer usable; the walk must restart from scratch.

    Graph answers 410 when a `deltaLink` has aged out or the mailbox changed
    underneath it. It is neither a failure nor a throttle: the recovery is to
    drop the stored token and re-walk, which only `delta.py` can decide to do.
    """


class GraphAuthError(GraphError):
    """The grant no longer covers this call — 401 or 403.

    Distinct from throttling because it answers the same way forever: the fix
    is re-consent, not patience. Retrying would bury the cause under exhausted
    attempts, which is precisely how the empty-scope bug stayed invisible.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Graph refused the request ({status_code}): {detail}")
        self.status_code = status_code


class GraphThrottled(GraphError):
    """Graph asked us to back off, or had a bad moment. Retry after the delay."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Graph throttled the request; retry after {retry_after}s")
        self.retry_after = retry_after


class GraphClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.GRAPH_BASE_URL,
            timeout=settings.GRAPH_TIMEOUT_SECONDS,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": IMMUTABLE_ID_HEADER,
            },
        )

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self, path: str, params: dict | None = None, headers: dict | None = None
    ) -> dict:
        """GET, with optional per-request headers.

        `headers` exists for `ConsistencyLevel: eventual`, which Graph requires
        before it will answer `$count` on a filtered message collection. It is
        per-request rather than on the client because setting it globally
        changes the consistency guarantees of every other call.
        """
        return self._unwrap(await self._client.get(path, params=params, headers=headers))

    async def post(self, path: str, json: dict) -> dict:
        return self._unwrap(await self._client.post(path, json=json))

    async def patch(self, path: str, json: dict) -> dict:
        return self._unwrap(await self._client.patch(path, json=json))

    async def delete(self, path: str) -> bool:
        """Delete, treating "already absent" as success.

        Subscription cleanup runs on paths that race — a recreate that beat us,
        a lifecycle event handled twice — and in every one of them a 404 means
        the desired state already holds. A real failure still raises.

        Returns whether the resource was actually there. Callers that only care
        about the end state can ignore it; the distinction exists because
        "we removed it" and "it was already gone" are worth different log
        lines when a subscription disappears unexpectedly.
        """
        response = await self._client.delete(path)
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 204):
            self._unwrap(response)
        return True

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict:
        """Classify a response into something a caller can act on.

        Every condition the pipeline must branch on gets its own exception
        type, so no consumer has to import httpx and read status codes. What
        remains — 400, 422, and friends — is a bug in the request we sent, not
        a runtime state to recover from, and `raise_for_status` is the right
        answer to a bug.
        """
        status = response.status_code
        if status == 404:
            raise GraphNotFound(response.text)
        if status == 410:
            raise GraphResyncRequired(response.text)
        if status in (401, 403):
            raise GraphAuthError(status, response.text[:200])
        # 5xx joins 429 deliberately: both mean "not now", and the job layer
        # handles them identically.
        if status == 429 or status >= 500:
            raise GraphThrottled(_retry_after(response))
        response.raise_for_status()
        return response.json() if response.content else {}


def _retry_after(response: httpx.Response) -> float:
    """Honour Retry-After when Graph sends a usable one.

    The header may also carry an HTTP-date. Rather than parse a second format
    for a value that is advisory anyway, an unparseable header falls back to the
    configured default — a throttle must not become a ValueError.
    """
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS)
