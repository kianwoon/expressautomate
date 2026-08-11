"""Run both arq workers in one process: the default queue and the interactive queue.

`arq app.workers.settings.WorkerSettings` launches a single worker against a
single queue. That one-queue shape is what let a replay/extraction backlog on
the default queue starve a recruiter's click: the analysis jobs shared the same
10-slot budget as the background flood, and every slot was taken by a stuck
DeepSeek call.

The fix is a second queue plus a second worker, and this module is the arq
entrypoint that runs both. `InteractiveWorkerSettings` consumes only
`ARQ_INTERACTIVE_QUEUE` with its own `max_jobs` budget, so the interactive
worker always has free slots for job/candidate intelligence no matter how deep
the default queue's backlog gets.

Signal handling is installed once here for both workers. arq's `Worker`
registers SIGINT/SIGTERM handlers per instance; two workers each registering
their own would leave the first-registered handler orphaned, so both are
created with `handle_signals=False` and one pair of handlers drives them
together. `create_worker` mirrors how the arq CLI builds a worker from a
settings class (the same `get_kwargs` machinery), just without its
single-worker `run_worker` wrapper.
"""

import asyncio
import signal

from arq.worker import create_worker

from app.workers.settings import InteractiveWorkerSettings, WorkerSettings


def _shutdown(workers: list, signum: signal.Signals) -> None:
    """Drive every worker's shutdown path from one signal handler.

    arq's per-worker handler cancels in-flight jobs and the worker's main task;
    calling it on each worker in turn is exactly what two independent handlers
    would have done, minus the orphaned-first-handler problem.
    """
    for worker in workers:
        worker.handle_sig(signum)


async def main() -> None:
    """Both workers, one event loop, one shutdown."""
    workers = [
        create_worker(WorkerSettings, handle_signals=False),
        create_worker(InteractiveWorkerSettings, handle_signals=False),
    ]

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, lambda s=signum: _shutdown(workers, s))

    try:
        await asyncio.gather(*(w.async_run() for w in workers))
    except asyncio.CancelledError:
        # A worker's handle_sig cancels its main task; async_run propagates the
        # CancelledError. Both workers are being stopped together, so this is
        # the normal shutdown path, not a failure.
        pass
    finally:
        for worker in workers:
            await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
