"""HTTP client to the Career bot external candidate-search service.

Follows the `app/services/wa_gateway.py` idiom: one client per call,
`httpx.HTTPError` caught rather than left to propagate, closed in `finally`.
The one addition mirrors the gateway's: `CareerBotUnreachableError` — where
the WA gateway answers a dead gateway with an API-only status, the career bot
search is a feature the recruiter explicitly clicked, so the same fact is
surfaced as a structured error the route renders as a sentence (never a 500;
CLAUDE.md's rule is explicit that an unreachable external system must not
500 the API).

The API key never appears in anything this module raises or returns to a
caller outside this process — no exception message names the URL or the key,
so a caller that logs or serialises an exception cannot leak either.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class CareerBotUnreachableError(Exception):
    """We never got a usable answer: connection refused/timed out, or the
    service returned a 5xx.

    Deliberately carries no URL or key — see the module docstring — so a
    caller that logs or serialises this exception cannot leak either.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CareerBotError(Exception):
    """The service answered with an error we can quote — the `message` in its
    error envelope (spec §5). `status` is the HTTP status it came with.

    `message` is bounded by the caller before being shown; it is the career
    bot's own wording, carried verbatim, because §15 forbids paraphrasing a
    reason we were actually given into something more plausible.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class CareerBotRateLimited(CareerBotError):
    """429 from the career bot: its per-key rate limit (default 30 req/min).
    `retry_after` is its `Retry-After`, in seconds, when it gave one."""

    def __init__(self, message: str, status: int, retry_after: int | None) -> None:
        super().__init__(message, status)
        self.retry_after = retry_after


# Everything a recruiter-facing sentence needs from the start call (spec §2,
# response 201). `status` is always `pending` here; the panel polls the task
# endpoint for state, so only `task_id` matters to the route.
@dataclass(frozen=True)
class StartedSearch:
    task_id: str


# The poll answer, in the career bot's own vocabulary (spec §3). `paused`
# means a human must take over on the service side — the spec says stop, not
# retry, so the value travels to the panel verbatim rather than being folded
# into "failed".
@dataclass(frozen=True)
class TaskStatus:
    status: str
    error: str | None


class CareerBotClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def start_search(self, payload: dict[str, Any]) -> StartedSearch:
        """POST /api/v1/search/candidates — 201 carries the task id."""
        body = await self._call("POST", "/api/v1/search/candidates", payload)
        return StartedSearch(task_id=body["task_id"])

    async def get_task(self, task_id: str) -> TaskStatus:
        """GET /api/v1/tasks/{task_id} — the poll the panel drives."""
        body = await self._call("GET", f"/api/v1/tasks/{task_id}", None)
        return TaskStatus(status=body["status"], error=body.get("error"))

    async def get_results(self, task_id: str) -> dict[str, Any]:
        """GET /api/v1/tasks/{task_id}/results — the ranked list (spec §4).

        Returned as-is: the shape is the product here (scores, evidence,
        gaps, credibility), and mirroring it field by field would give a
        second copy of the spec that drifts the first time the service adds
        a field. The route hands it to the browser unchanged.
        """
        return await self._call("GET", f"/api/v1/tasks/{task_id}/results", None)

    async def _call(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(
            timeout=settings.CAREER_BOT_TIMEOUT_SECONDS
        )
        headers = {"X-API-Key": settings.CAREER_BOT_API_KEY}
        url = f"{settings.CAREER_BOT_URL}{path}"
        try:
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # Never a URL, never the key — see the module docstring.
            log.warning(
                "career_bot_transport_error", path=path, error=type(exc).__name__
            )
            raise CareerBotUnreachableError(
                "the external candidate search service could not be reached"
            ) from exc
        finally:
            if self._owns_client:
                try:
                    await client.aclose()
                except httpx.HTTPError:
                    pass

        if response.status_code >= 500:
            log.warning("career_bot_5xx", path=path, status_code=response.status_code)
            raise CareerBotUnreachableError(
                "the external candidate search service returned a server error"
            )
        if response.status_code == 429:
            raise CareerBotRateLimited(
                _error_message(response), 429, _retry_after(response)
            )
        if response.status_code >= 400:
            raise CareerBotError(_error_message(response), response.status_code)
        return _json(response)

    # The career bot's search takes minutes and is polled — that is its own
    # design, not ours. Nothing here polls on the caller's behalf: the panel
    # owns the cadence, exactly as the sourcing panel does.


# Anything longer is a service bug or a hostile payload, not a reason; and the
# column is read back into a browser.
_MAX_MESSAGE_CHARS = 500


def _error_message(response: httpx.Response) -> str:
    """The career bot's error-envelope `message`, verbatim apart from a bound.

    No paraphrase, no mapping to one of our own sentences (§15). If it sent
    something unparseable we say that, rather than inventing a plausible
    cause. Never names the key or URL — they cannot appear in the envelope
    we did not write, and nothing here adds them.
    """
    try:
        body = response.json()
    except ValueError:
        return "The external candidate search service refused without saying why."
    envelope = body.get("error") if isinstance(body, dict) else None
    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, str) or not message.strip():
        return "The external candidate search service refused without saying why."
    return message.strip()[:_MAX_MESSAGE_CHARS]


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except ValueError:
        return None


def _json(response: httpx.Response) -> dict[str, Any]:
    """The body, or an unreachable-error rather than a crash on bad JSON.

    A 200 whose body will not parse is the service failing, and treating it
    as one keeps the recruiter's sentence the same shape whichever way the
    service breaks.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise CareerBotUnreachableError(
            "the external candidate search service sent an unreadable answer"
        ) from exc
    if not isinstance(body, dict):
        raise CareerBotUnreachableError(
            "the external candidate search service sent an unreadable answer"
        )
    return body
