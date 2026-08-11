"""What the deployment must contain for the pipeline to actually run.

Two failure modes motivate this file, and neither produces an error anywhere.

A process that is never deployed: the webhook accepts notifications, the
supervisor sweeps, both enqueue jobs — and nothing consumes the queue, so every
email sits at `pending` while Redis fills up. Everything reports success.

A setting nobody declares: it falls back to a default that is wrong for
production, or to an empty string that disables a feature silently. That has
already happened once here — `MS_GRAPH_SCOPES` drifted and every sign-in
requested no permissions at all.
"""

import re
from pathlib import Path

import pytest

from app.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "backend.yml"
ENV_EXAMPLE = REPO / ".env.example"
DOCKERFILE = REPO / "Dockerfile"

# The three processes the running system needs. They share one image and differ
# only by command — which is why the Koyeb service is the thing to assert on:
# `api` runs the image's default CMD, so its command appears nowhere in the
# workflow, while the other two are overridden there.
SERVICES = {
    # Serves the site and the API, and receives Graph notifications.
    "expressautomate/api": "uvicorn app.main:app",
    # Periodic recovery: rescan_stuck, renew_subscriptions, delta_sync_all,
    # ensure_subscriptions.
    "expressautomate/worker": "app.workers.main",
    # Drains the queue. Without it nothing the other two enqueue ever runs.
    # Runs both arq workers in one process: the default queue and the
    # interactive queue (job/candidate intelligence).
    "expressautomate/arq": "app.workers.run_arq",
}


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_every_process_the_system_needs_is_deployed(service):
    """A process that is never deployed fails silently and completely.

    The queue consumer is the dangerous one: producers report success, rows
    accumulate at `pending`, and the only symptom is mail that never appears.
    """
    assert service in WORKFLOW.read_text(), (
        f"{service} is never deployed by .github/workflows/backend.yml"
    )


def test_a_missing_graph_url_is_answerable_rather_than_a_crash():
    """Found in production, on the day the web service first called Graph.

    `GRAPH_BASE_URL` was set on both workers and not on `api`. Empty, it makes
    httpx build a URL with no host, which fails deep in the transport — past
    every `except` clause that could name the cause — so the endpoint returned
    a bare 500 for what was a one-line deployment gap.

    The empty default stays (three services share one image and only some need
    Graph), so what has to hold is that the emptiness is *detectable*.
    """
    assert not Settings(GRAPH_BASE_URL="").graph_configured()
    assert Settings(GRAPH_BASE_URL="https://graph.microsoft.com/v1.0").graph_configured()


@pytest.mark.parametrize("command", sorted(SERVICES.values()))
def test_the_dockerfile_documents_how_each_process_starts(command):
    """One image, three commands. Whoever recreates a Koyeb service by hand
    needs to find them without reverse-engineering the workflow."""
    assert command in DOCKERFILE.read_text(), (
        f"{command!r} is not documented in the Dockerfile"
    )


def test_every_setting_is_discoverable_in_the_env_example():
    """An operator configures from `.env.example`; anything absent from it is a
    setting they will never know to set, running on a default nobody chose.

    A field with an `alias=` is only ever populated from that alias — the
    Python field name is dead as an env key (see `FREE_EMAIL_DOMAINS_RAW`,
    aliased to `FREE_EMAIL_DOMAINS`) — so the alias, not the field name, is
    the key that must actually appear in `.env.example`.
    """
    example = ENV_EXAMPLE.read_text()
    env_names = {
        (field.alias or name): name for name, field in Settings.model_fields.items()
    }

    missing = sorted(
        field_name
        for env_name, field_name in env_names.items()
        if not re.search(rf"^{env_name}=", example, re.M)
    )

    assert missing == [], f"settings absent from .env.example: {missing}"


def test_settings_without_a_default_are_marked_required_in_the_example():
    """A required setting left blank stops the app at startup, which is the
    right behaviour — but only if the operator was told to fill it in."""
    required = {
        name
        for name, field in Settings.model_fields.items()
        if field.is_required()
    }

    assert required, "sanity: some settings must be required"
    example = ENV_EXAMPLE.read_text()
    for name in required:
        assert re.search(rf"^{name}=", example, re.M), f"{name} is required but absent"


def test_the_queue_worker_configures_its_own_logging():
    """Both arq workers configure logging on startup.

    arq is launched against our settings classes, so no entrypoint of ours runs
    first for the default worker — `run_arq` runs first, but each worker's
    `on_startup` still configures logging so a worker started by the arq CLI
    directly (or one that outlives the entrypoint's own setup) never falls back
    to structlog's console defaults while every other process emits JSON — the
    kind of difference that gets a real error skimmed past in a log pipeline.
    """
    from app.workers.settings import (
        InteractiveWorkerSettings,
        WorkerSettings,
    )

    for settings_cls in (WorkerSettings, InteractiveWorkerSettings):
        assert settings_cls.on_startup is not None
        assert "configure_logging" in settings_cls.on_startup.__code__.co_names


def test_the_interactive_worker_consumes_only_the_interactive_queue():
    """The interactive worker exists so a user's click is never starved behind
    a background backlog. It must run only the two analysis jobs, on the
    interactive queue, with its own slot budget — the three properties that
    make the isolation real rather than nominal.
    """
    from app.core.config import settings
    from app.workers.settings import InteractiveWorkerSettings

    names = {
        getattr(fn, "name", None) or fn.__name__ for fn in InteractiveWorkerSettings.functions
    }
    assert names == {"run_job_intelligence", "run_candidate_intelligence"}
    assert InteractiveWorkerSettings.queue_name == settings.ARQ_INTERACTIVE_QUEUE
    assert InteractiveWorkerSettings.max_jobs == settings.ARQ_INTERACTIVE_MAX_JOBS
