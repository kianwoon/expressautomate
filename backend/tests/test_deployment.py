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
    "expressautomate/arq": "app.workers.settings.WorkerSettings",
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
    """
    declared = set(Settings.model_fields)
    example = ENV_EXAMPLE.read_text()

    missing = sorted(name for name in declared if not re.search(rf"^{name}=", example, re.M))

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
