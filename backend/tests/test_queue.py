"""The arq queue plumbing (plan §7).

One behaviour dominates this module: **enqueue fails soft**.

Redis cannot join the Postgres transaction that just committed the row, so
there is an unavoidable gap between "the email is durably recorded" and "a job
exists to process it". Raising into the webhook would turn that gap into a 500
on a request that has already done its durable work — Graph would then retry a
notification whose row already exists, and the user would still see nothing.
Returning False and letting `rescan_stuck` sweep the row is the design.

Nothing here talks to Redis; the pool is always injected or faked.
"""

import pytest
import structlog.testing

from app.core.config import settings
from app.workers import queue


@pytest.fixture(autouse=True)
def _reset_pool():
    """The pool is cached across calls; each test starts from a clean one."""
    queue.reset_pool()
    yield
    queue.reset_pool()


class _FakePool:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs):
        self.jobs.append((name, kwargs))
        return object()

    async def aclose(self) -> None:
        return None


async def test_a_queued_job_reaches_redis_with_its_arguments(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(queue, "_create_pool", _returns(pool))

    assert await queue.enqueue("fetch_email", email_message_id="abc") is True
    assert pool.jobs == [("fetch_email", {"email_message_id": "abc"})]


async def test_an_interactive_job_is_routed_to_its_own_queue(monkeypatch):
    """User-initiated analyses enqueue to the interactive queue, not the
    default one — that is what lets a dedicated worker with its own slot
    budget pick them up regardless of a background replay backlog."""

    pool = _FakePool()
    monkeypatch.setattr(queue, "_create_pool", _returns(pool))

    assert (
        await queue.enqueue(
            "run_job_intelligence",
            queue_name=settings.ARQ_INTERACTIVE_QUEUE,
            tenant_id="t",
            opportunity_id="o",
            row_id="r",
        )
        is True
    )
    assert pool.jobs == [
        (
            "run_job_intelligence",
            {
                "tenant_id": "t",
                "opportunity_id": "o",
                "row_id": "r",
                "_queue_name": settings.ARQ_INTERACTIVE_QUEUE,
            },
        )
    ]


async def test_enqueue_reports_failure_instead_of_raising(monkeypatch):
    """The webhook must still answer 202. Its durable work is already done, and
    `rescan_stuck` is what closes the gap."""

    async def unreachable():
        raise ConnectionError("no redis")

    monkeypatch.setattr(queue, "_create_pool", unreachable)

    assert await queue.enqueue("fetch_email", email_message_id="abc") is False


async def test_a_failure_to_enqueue_is_logged(monkeypatch):
    """Failing soft must not mean failing silently — this is the only signal
    that the recovery sweep is doing work it should not have to.

    Captured through structlog rather than `caplog`: this project configures
    structlog to render directly rather than routing through stdlib logging,
    so a `caplog` assertion would pass vacuously by recording nothing.
    """

    async def unreachable():
        raise ConnectionError("no redis")

    monkeypatch.setattr(queue, "_create_pool", unreachable)

    with structlog.testing.capture_logs() as logs:
        await queue.enqueue("fetch_email", email_message_id="abc")

    failures = [entry for entry in logs if entry["event"] == "enqueue_failed"]
    assert len(failures) == 1
    assert failures[0]["job"] == "fetch_email"
    assert failures[0]["log_level"] == "error"


async def test_a_failure_inside_enqueue_job_also_fails_soft(monkeypatch):
    """The pool can be healthy and the call still fail — a dropped connection
    mid-command looks different from a pool that never opened."""

    class _BrokenPool(_FakePool):
        async def enqueue_job(self, name: str, **kwargs):
            raise ConnectionError("connection reset")

    monkeypatch.setattr(queue, "_create_pool", _returns(_BrokenPool()))

    assert await queue.enqueue("fetch_email", email_message_id="abc") is False


async def test_the_pool_is_opened_once_and_reused(monkeypatch):
    """Upstash bills per command and caps connections. Opening a pool per
    notification would add a TLS handshake to a webhook with a three-second
    budget, and burn a connection slot each time.
    """
    opened = []

    async def _create():
        opened.append(1)
        return _FakePool()

    monkeypatch.setattr(queue, "_create_pool", _create)

    for _ in range(5):
        await queue.enqueue("fetch_email", email_message_id="x")

    assert len(opened) == 1


async def test_a_failed_pool_is_not_cached(monkeypatch):
    """A Redis blip must not wedge the queue for the life of the process."""
    attempts = []

    async def _flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("first attempt fails")
        return _FakePool()

    monkeypatch.setattr(queue, "_create_pool", _flaky)

    assert await queue.enqueue("fetch_email", email_message_id="x") is False
    assert await queue.enqueue("fetch_email", email_message_id="x") is True
    assert len(attempts) == 2


def test_the_redis_dsn_keeps_its_transport_security():
    """The DSN carries a password. Parsing that must not quietly downgrade a
    `rediss://` URL to plaintext, which would put the credential on the wire.
    """
    if not settings.REDIS_URL.startswith("rediss://"):
        pytest.skip("no TLS Redis configured in this environment")

    assert queue.redis_settings().ssl is True


async def test_a_deduplicated_job_is_not_reported_as_queued(monkeypatch):
    """arq answers None when the job id already exists.

    No caller passes `_job_id` today, so arq generates a uuid and this cannot
    fire — but reporting True for a job that was never created would leave the
    caller believing work is queued while the row silently waits for
    `rescan_stuck`.
    """

    class _DedupingPool(_FakePool):
        async def enqueue_job(self, name: str, **kwargs):
            await super().enqueue_job(name, **kwargs)
            return None

    monkeypatch.setattr(queue, "_create_pool", _returns(_DedupingPool()))

    assert await queue.enqueue("fetch_email", email_message_id="abc") is False


def test_the_poll_delay_is_slow_enough_not_to_bill_by_the_second():
    """arq polls, and Upstash charges per command, so a tight loop costs money
    every second the system is idle.

    This guards the deployed value, not the default: it fires if someone sets
    ARQ_POLL_DELAY_SECONDS low in `.env` to shave latency, which is a costly
    trade in a pipeline whose slowest step is an LLM call.
    """
    assert settings.ARQ_POLL_DELAY_SECONDS >= 1.0


def _returns(value):
    async def _factory():
        return value

    return _factory
