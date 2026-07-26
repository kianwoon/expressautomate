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


async def _heartbeat() -> None:
    """Placeholder until the first real task lands, so idling is visible."""
    log.info("worker_idle", reason="no ingestion tasks registered yet")


def build_tasks() -> list[PeriodicTask]:
    """Registry of periodic work.

    Stage 2 adds subscription renewal (§8) and delta reconciliation (§9);
    Stage 3 adds the queue drain (§7).
    """
    return [PeriodicTask(name="heartbeat", interval_seconds=300.0, run=_heartbeat)]


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

    try:
        await asyncio.gather(*(_run_periodically(t, stop) for t in tasks))
    finally:
        await engine.dispose()
        log.info("worker_shutdown")


if __name__ == "__main__":
    asyncio.run(main())
