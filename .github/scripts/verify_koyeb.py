#!/usr/bin/env python3
"""Confirm the deployed image is the one this run built.

A deploy that reports success while production keeps running the previous
build already happened once here: a raw API PATCH returned 2xx and changed
nothing. The CLI exit code is likewise not proof — it returns before the
rollout completes. This reads the state back.

Standard library only: the deploy job holds production credentials, so it
installs nothing.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://app.koyeb.com/v1"
POLL_SECONDS = 5
MAX_ATTEMPTS = 60  # five minutes — a cold image pull is slower than a definition write

# Terminal states: no amount of waiting improves these, so fail immediately
# rather than burning the whole timeout.
FAILED_STATUSES = frozenset(
    {"ERROR", "ERRORING", "CANCELED", "CANCELING", "STASHED", "STOPPED", "STOPPING"}
)
HEALTHY_STATUSES = frozenset({"HEALTHY"})


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"Koyeb GET {path} failed: HTTP {e.code}\n{detail}") from e


def confirm(service_id: str, image: str, token: str, min_env: int = 0) -> None:
    name = service_id
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        service = _get(f"/services/{service_id}", token)["service"]
        name = service["name"]
        deployment = _get(f"/deployments/{service['latest_deployment_id']}", token)["deployment"]
        definition = deployment["definition"]
        status = deployment.get("status", "")

        running = (definition.get("docker") or {}).get("image")
        stale_source = bool(definition.get("archive") or definition.get("git"))
        env_count = len(definition.get("env", []))
        on_target = running == image and not stale_source

        # Checking the definition alone is not enough. A deployment can carry
        # the right image and still never serve it — an expired registry
        # credential fails the pull, the deployment goes ERROR, and the
        # previous build keeps handling traffic. That is the same
        # silent-success this script exists to catch, one layer down.
        if on_target and status in FAILED_STATUSES:
            raise SystemExit(
                f"{name}: deployment for {image} ended in {status}. Production is still "
                "serving the previous build. Check `koyeb service logs` — an expired "
                "GHCR credential fails exactly this way."
            )

        if on_target and status in HEALTHY_STATUSES:
            # Opt-in: the backend must never boot without DATABASE_URL, but the
            # static frontend legitimately carries no environment at all.
            if env_count < min_env:
                raise SystemExit(
                    f"{name}: image is correct but only {env_count} environment "
                    f"variables are set, expected at least {min_env} — the service "
                    "would boot without its configuration."
                )
            print(f"{name}: running {running} ({env_count} env vars, {status})")
            return

        last = f"image={running!r} status={status!r} archive={bool(definition.get('archive'))}"
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(POLL_SECONDS)

    raise SystemExit(
        f"{name}: {image} did not become healthy within {MAX_ATTEMPTS * POLL_SECONDS}s — "
        f"last saw {last}. Production may still be running a different build."
    )


def main() -> int:
    token = os.environ["KOYEB_TOKEN"]
    # Content-addressed: the tag is a hash of backend/, frontend/ and the
    # Dockerfile, so a commit that changes none of them reuses the published
    # image instead of rebuilding it under a new commit-SHA tag.
    image = f"{os.environ['IMAGE']}:{os.environ['IMAGE_TAG']}"
    # Comma-separated so the same script serves both workflows: the backend
    # deploys two services from one image, the frontend one.
    service_ids = [s.strip() for s in os.environ["SERVICE_IDS"].split(",") if s.strip()]
    if not service_ids:
        raise SystemExit("SERVICE_IDS is empty — nothing would be verified.")
    # Required, not defaulted. A default of 0 means a typo like MIN_ENV_VAR
    # silently turns the floor into "0 or more" — always true. This script
    # exists because a deploy once reported success while changing nothing;
    # its own guards must fail loudly when unset.
    try:
        min_env = int(os.environ["MIN_ENV_VARS"])
    except KeyError:
        raise SystemExit(
            "MIN_ENV_VARS is not set. Set it explicitly per workflow — 0 for a "
            "static site, or the number of variables the service cannot boot without."
        ) from None
    for service_id in service_ids:
        confirm(service_id, image, token, min_env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
