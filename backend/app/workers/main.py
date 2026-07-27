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
    from app.workers.tasks import delta_sync_all, renew_subscriptions, rescan_stuck

    async def _rescan() -> None:
        await rescan_stuck()

    async def _renew() -> None:
        await renew_subscriptions()

    async def _delta() -> None:
        await delta_sync_all()

    return [
        PeriodicTask("rescan_stuck", settings.RESCAN_INTERVAL_SECONDS, _rescan),
        PeriodicTask("renew_subscriptions", settings.RENEW_INTERVAL_SECONDS, _renew),
        PeriodicTask("delta_sync", settings.DELTA_SYNC_INTERVAL_SECONDS, _delta),
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
