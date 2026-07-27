"""The Microsoft Graph HTTP client (plan §7, §9).

The client is deliberately thin: it owns authentication, the immutable-ID
preference, and turning Graph's two interesting failure codes into exceptions
callers can branch on. Retries, backoff and concurrency belong to the job
layer, which knows whether a retry is worth paying for.

Every test here drives a `MockTransport`, so the suite never reaches the
network.
"""

import httpx
import pytest

from app.core.config import settings
from app.services.graph.client import (
    IMMUTABLE_ID_HEADER,
    GraphAuthError,
    GraphClient,
    GraphError,
    GraphNotFound,
    GraphResyncRequired,
    GraphThrottled,
)


def _client(handler) -> GraphClient:
    return GraphClient(token="fake-token", transport=httpx.MockTransport(handler))


def _responds(status: int, *, json=None, headers=None):
    return lambda request: httpx.Response(status, json=json if json is not None else {},
                                          headers=headers or {})


async def test_immutable_id_and_auth_headers_are_always_sent():
    """Graph message ids change when a message moves folders, which would break
    every dedup constraint keyed on them."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["prefer"] = request.headers.get("Prefer")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "AAA"})

    result = await _client(handler).get("/me/messages/AAA")

    assert result == {"id": "AAA"}
    assert seen["prefer"] == IMMUTABLE_ID_HEADER
    assert seen["auth"] == "Bearer fake-token"


async def test_query_parameters_reach_graph():
    """`$select` is how the fetch job avoids pulling fields it will not store."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    await _client(handler).get("/me/messages", params={"$select": "id,subject"})

    assert "%24select=id%2Csubject" in seen["url"] or "$select=id,subject" in seen["url"]


async def test_an_absolute_url_is_followed_with_headers_intact():
    """Delta pagination hands back a full `@odata.nextLink`, not a path.

    If `base_url` were prepended to it the walk would 404 on page two, and if
    the headers were dropped it would 401 — both only on mailboxes large enough
    to paginate, which is the worst possible time to find out.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["prefer"] = request.headers.get("Prefer")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"value": []})

    next_link = "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=abc123"
    await _client(handler).get(next_link)

    assert seen["url"] == next_link
    assert seen["prefer"] == IMMUTABLE_ID_HEADER
    assert seen["auth"] == "Bearer fake-token"


async def test_404_raises_graph_not_found():
    """A deleted message is terminal — retrying it never starts working."""
    with pytest.raises(GraphNotFound):
        await _client(_responds(404)).get("/me/messages/GONE")


async def test_429_carries_the_retry_after_graph_asked_for():
    with pytest.raises(GraphThrottled) as excinfo:
        await _client(_responds(429, headers={"Retry-After": "17"})).get("/me/messages")

    assert excinfo.value.retry_after == 17.0


async def test_429_without_a_header_falls_back_to_the_configured_default():
    with pytest.raises(GraphThrottled) as excinfo:
        await _client(_responds(429)).get("/me/messages")

    assert excinfo.value.retry_after == float(settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS)


async def test_a_nonsense_retry_after_falls_back_rather_than_crashing():
    """Graph may send an HTTP-date instead of seconds. Failing to parse it must
    not turn a retryable throttle into an unhandled ValueError."""
    with pytest.raises(GraphThrottled) as excinfo:
        await _client(
            _responds(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        ).get("/me/messages")

    assert excinfo.value.retry_after == float(settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS)


@pytest.mark.parametrize("status", [500, 503])
async def test_server_errors_are_retryable_like_throttling(status):
    """A 5xx is Graph having a bad day, not a permanent answer."""
    with pytest.raises(GraphThrottled):
        await _client(_responds(status)).get("/me/messages")


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_their_own_kind_of_error(status):
    """A missing Mail.Read scope returns 403 forever. Classifying it as a
    throttle would retry it until the job gave up, burying the real cause —
    which is exactly how the empty-scope bug stayed invisible.
    """
    with pytest.raises(GraphAuthError) as excinfo:
        await _client(_responds(status)).get("/me/messages")

    assert excinfo.value.status_code == status


async def test_an_expired_delta_token_asks_for_a_resync():
    """Graph answers 410 when a deltaLink has aged out. That is a recovery
    instruction — drop the token and re-walk — not a failure, and only the
    delta sync can act on it."""
    with pytest.raises(GraphResyncRequired):
        await _client(_responds(410)).get("/me/mailFolders/x/messages/delta")


async def test_callers_never_have_to_read_a_status_code():
    """Every condition the pipeline branches on is a GraphError subclass, so no
    consumer needs to import httpx to make a decision."""
    for status in (404, 410, 401, 403, 429, 503):
        with pytest.raises(GraphError):
            await _client(_responds(status)).get("/me/messages")


async def test_delete_tolerates_an_already_deleted_subscription():
    """Subscription cleanup must be idempotent: recreate-then-delete races, and
    a 404 there means the desired state already holds."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(404, json={})

    await _client(handler).delete("/subscriptions/gone")

    # Assert the call happened: a `delete` that never issued a request would
    # also raise nothing, and would pass a bare no-exception check. The path is
    # matched by suffix because base_url carries the API version prefix.
    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/subscriptions/gone")


async def test_delete_still_raises_on_a_real_failure():
    with pytest.raises(GraphThrottled):
        await _client(_responds(503)).delete("/subscriptions/x")


async def test_an_empty_response_body_is_not_a_parse_error():
    """PATCH and DELETE answer 204 with no body."""
    result = await _client(lambda r: httpx.Response(204)).patch("/subscriptions/x", json={})

    assert result == {}


async def test_the_client_closes_as_a_context_manager():
    async with _client(_responds(200)) as client:
        await client.get("/me")
    # Exiting must not raise; the job layer relies on `async with` for cleanup.


def test_the_graph_base_url_is_configured():
    """Every request path in this module is relative to it.

    Left empty, httpx cannot resolve them at all — `ValueError: unknown url
    type: '/me/messages'` — and no amount of retrying helps. Asserted on
    resolved settings so it fails in CI too, the same way the scope keys are
    checked in tests/test_scopes.py.
    """
    assert settings.GRAPH_BASE_URL.startswith("https://"), (
        "GRAPH_BASE_URL must be set (see .env.example and the workflow env block)"
    )
