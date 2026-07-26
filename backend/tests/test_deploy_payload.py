"""Tests for the Koyeb deploy payload transform.

This script is dormant — it runs only once image-based deploys are switched on,
which is exactly when nobody will be watching it closely. A review found three
defects in it while it sat unused: a missing `type` discriminator, a
short-circuit that left a second source block in the payload, and empty-string
run values that would blank the worker's command. These pin all three.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "deploy_koyeb.py"
_spec = importlib.util.spec_from_file_location("deploy_koyeb", _SCRIPT)
assert _spec and _spec.loader
deploy_koyeb = importlib.util.module_from_spec(_spec)
sys.modules["deploy_koyeb"] = deploy_koyeb
_spec.loader.exec_module(deploy_koyeb)

build_payload = deploy_koyeb.build_payload

IMAGE = "ghcr.io/kianwoon/expressautomate-api:sha-abc"
SECRET = "ghcr-expressautomate"

API_DEFINITION = {
    "name": "api",
    "type": "ARCHIVE",
    "archive": {"id": "x", "docker": {"dockerfile": "Dockerfile", "command": "", "args": []}},
    "env": [{"key": "DATABASE_URL", "value": "..."}],
    "ports": [{"port": 8000, "protocol": "http"}],
    "scalings": [{"scopes": ["region:was"], "min": 1, "max": 1}],
}

WORKER_DEFINITION = {
    "name": "worker",
    "type": "ARCHIVE",
    "archive": {
        "id": "y",
        "docker": {
            "dockerfile": "Dockerfile",
            "command": "python",
            "args": ["-u", "-m", "app.workers.main"],
        },
    },
    "env": [{"key": "DATABASE_URL", "value": "..."}],
    "scalings": [{"scopes": ["region:was"], "min": 1, "max": 1}],
}


def test_source_is_switched_to_docker() -> None:
    out = build_payload(API_DEFINITION, IMAGE, SECRET)
    # `type` is the SERVICE type (WEB/WORKER), not a source discriminator.
    # Overwriting it with "DOCKER" was silently ignored by Koyeb.
    assert out["type"] == API_DEFINITION["type"]
    # Explicit nulls, not omission: PATCH merges, so an omitted `archive`
    # leaves the old source in place and the deploy no-ops.
    assert out["archive"] is None and out["git"] is None
    assert out["docker"]["image"] == IMAGE
    assert out["docker"]["image_registry_secret"] == SECRET


def test_everything_else_is_preserved() -> None:
    out = build_payload(API_DEFINITION, IMAGE, SECRET)
    assert out["env"] == API_DEFINITION["env"]
    assert out["ports"] == API_DEFINITION["ports"]
    assert out["scalings"] == API_DEFINITION["scalings"]


def test_worker_keeps_its_command() -> None:
    """Losing this starts a second copy of the API instead of the worker."""
    out = build_payload(WORKER_DEFINITION, IMAGE, SECRET)
    assert out["docker"]["command"] == "python"
    assert out["docker"]["args"] == ["-u", "-m", "app.workers.main"]


def test_empty_run_values_do_not_become_a_command() -> None:
    out = build_payload(API_DEFINITION, IMAGE, SECRET)
    assert "command" not in out["docker"], "empty string must not shadow the image CMD"
    assert "args" not in out["docker"]


def test_every_source_block_is_removed_even_when_several_are_present() -> None:
    """A short-circuiting pop left one behind, producing an ambiguous payload."""
    messy = {
        **WORKER_DEFINITION,
        "git": {"repository": "example", "docker": {"command": "wrong"}},
        "docker": {"image": "stale:tag"},
    }
    out = build_payload(messy, IMAGE, SECRET)
    assert out["archive"] is None and out["git"] is None
    assert out["docker"]["image"] == IMAGE, "a stale docker block must not survive"
    assert out["docker"]["command"] == "python", "archive is read before git"


def test_null_source_blocks_do_not_crash() -> None:
    """The API emits explicit nulls for unset fields."""
    out = build_payload(
        {**WORKER_DEFINITION, "git": None, "docker": None}, IMAGE, SECRET
    )
    assert out["docker"]["command"] == "python"


def test_input_is_not_mutated() -> None:
    original = dict(WORKER_DEFINITION)
    build_payload(WORKER_DEFINITION, IMAGE, SECRET)
    assert WORKER_DEFINITION == original


@pytest.mark.parametrize("definition", [API_DEFINITION, WORKER_DEFINITION])
def test_payload_is_json_serialisable(definition: dict) -> None:
    import json

    json.dumps(build_payload(definition, IMAGE, SECRET))
