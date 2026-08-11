"""Background worker entrypoint (plan §7, §8, §9).

Runs the jobs that must not happen inside a web request:

- draining the ingestion queue and fetching each email via Microsoft Graph
- renewing Graph subscriptions before they expire
- periodic delta reconciliation to recover missed notifications

None of those exist yet — Stage 2 and 3 build them. What ships here is the
supervisor loop they will register into, so the process is deployable, exits
cleanly on SIGTERM, and reports what it is (and is not) doing rather than
pretending to work.
"""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.rls import verify_rls_enforced
from app.db.session import engine
from app.services.graph.client import warn_if_unconfigured

log = get_logger(__name__)


@dataclass(frozen=True)
class PeriodicTask:
    name: str
    interval_seconds: float
    run: Callable[[], Awaitable[None]]


def build_tasks() -> list[PeriodicTask]:
    """Registry of periodic work (plan §8, §9).

    This process makes sure work *exists*; arq processes it. Keeping them in
    separate processes means a wedged arq worker still gets fresh work queued,
    and a crashed supervisor does not stop work already in the queue.

    Imported inside the function so the module stays importable — and its
    supervisor loop testable — without the ingestion stack behind it.
    """
    from app.workers.tasks import (
        classify_fetched,
        delta_sync_all,
        ensure_backfills,
        ensure_subscriptions,
        flush_notifications,
        renew_subscriptions,
        replay_stale_extractions,
        rescan_stuck,
        sweep_stale_client_discovery_runs,
        sweep_stale_wa_sends,
        sweep_wa_liveness,
    )

    async def _rescan() -> None:
        await rescan_stuck()

    async def _classify() -> None:
        # On its own clock, and a fast one: this is the only thing that starts
        # classification now that `fetch_email` enqueues nothing, so its
        # interval is the latency every email waits before the gate sees it.
        await classify_fetched()

    async def _renew() -> None:
        await renew_subscriptions()

    async def _delta() -> None:
        await delta_sync_all()

    async def _ensure() -> None:
        # Both backstops for a lost enqueue, on the same clock: a mailbox with
        # no subscription, and one whose history was never walked.
        await ensure_subscriptions()
        await ensure_backfills()

    async def _flush_notifications() -> None:
        # Three duties on one clock: a delivery whose enqueue was lost, a
        # rate-capped batch with no later message to carry its "+N more", and
        # a claim never released because the worker holding it was killed
        # outright before any exception handler could run.
        await flush_notifications()

    async def _sweep_wa_sends() -> None:
        # Plan §6's liveness sweep, pending-row half: a WA gateway send whose
        # owning process died before it could resolve the row. See
        # `sweep_stale_wa_sends` in `app/workers/tasks.py`.
        await sweep_stale_wa_sends()

    async def _sweep_discovery_runs() -> None:
        # A client-discovery run is a user-facing button, not pipeline state,
        # but a run whose enqueue was lost or whose worker died would otherwise
        # sit `pending`/`running` until the recruiter happened to scan again —
        # the sweep parks it in `failed` with words to act on. See
        # `sweep_stale_client_discovery_runs` in `app/workers/tasks.py`.
        await sweep_stale_client_discovery_runs()

    async def _sweep_wa_liveness() -> None:
        # Plan §6's liveness sweep, background-check half: asks the gateway
        # about every `connected`/`reconnecting` session so a dead socket is
        # noticed even with no browser open. See `sweep_wa_liveness` in
        # `app/workers/tasks.py`.
        await sweep_wa_liveness()

    async def _replay() -> None:
        # Re-read emails extracted under an older prompt so a prompt upgrade
        # refreshes historical rows, not just new ones. Its own clock: replay
        # costs a model call per email, so it drains a backlog gradually.
        await replay_stale_extractions()

    return [
        PeriodicTask("rescan_stuck", settings.RESCAN_INTERVAL_SECONDS, _rescan),
        PeriodicTask(
            "classify_fetched", settings.CLASSIFY_SWEEP_INTERVAL_SECONDS, _classify
        ),
        PeriodicTask("renew_subscriptions", settings.RENEW_INTERVAL_SECONDS, _renew),
        PeriodicTask("delta_sync", settings.DELTA_SYNC_INTERVAL_SECONDS, _delta),
        PeriodicTask(
            "ensure_subscriptions", settings.ENSURE_SUBSCRIPTIONS_INTERVAL_SECONDS, _ensure
        ),
        PeriodicTask(
            "flush_notifications",
            settings.NOTIFY_SWEEP_INTERVAL_SECONDS,
            _flush_notifications,
        ),
        PeriodicTask(
            "sweep_stale_wa_sends",
            settings.WA_SWEEP_INTERVAL_SECONDS,
            _sweep_wa_sends,
        ),
        PeriodicTask(
            "sweep_stale_client_discovery_runs",
            settings.CLIENT_DISCOVERY_SWEEP_INTERVAL_SECONDS,
            _sweep_discovery_runs,
        ),
        PeriodicTask(
            "sweep_wa_liveness",
            settings.WA_LIVENESS_SWEEP_INTERVAL_SECONDS,
            _sweep_wa_liveness,
        ),
        PeriodicTask(
            "replay_stale_extractions",
            settings.REPLAY_SWEEP_INTERVAL_SECONDS,
            _replay,
        ),
    ]


async def _run_periodically(task: PeriodicTask, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await task.run()
        except Exception:
            # One failing task must never take the whole worker down.
            log.exception("worker_task_failed", task=task.name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=task.interval_seconds)
        except TimeoutError:
            continue


async def main() -> None:
    configure_logging()
    await verify_rls_enforced()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = build_tasks()
    log.info("worker_startup", env=settings.APP_ENV, tasks=[t.name for t in tasks])
    # Every sweep here ends in a Graph call; without the URL they all fail
    # obscurely, one job at a time, with nothing naming the cause.
    warn_if_unconfigured("worker")

    running = [asyncio.create_task(_run_periodically(t, stop)) for t in tasks]
    try:
        # return_exceptions keeps one unexpected failure from tearing the others
        # down mid-flight and disposing the engine underneath them; siblings are
        # cancelled deliberately below instead.
        results = await asyncio.gather(*running, return_exceptions=True)
        for task, outcome in zip(tasks, results, strict=True):
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                log.error("worker_task_crashed", task=task.name, error=repr(outcome))
    finally:
        stop.set()
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await engine.dispose()
        log.info("worker_shutdown")


if __name__ == "__main__":
    asyncio.run(main())
