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
MAX_ATTEMPTS = 24  # two minutes


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"Koyeb GET {path} failed: HTTP {e.code}\n{detail}") from e


def confirm(service_id: str, image: str, token: str) -> None:
    name = service_id
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        service = _get(f"/services/{service_id}", token)["service"]
        name = service["name"]
        definition = _get(f"/deployments/{service['latest_deployment_id']}", token)[
            "deployment"
        ]["definition"]

        running = (definition.get("docker") or {}).get("image")
        stale_source = bool(definition.get("archive") or definition.get("git"))
        env_count = len(definition.get("env", []))

        if running == image and not stale_source:
            if not env_count:
                raise SystemExit(
                    f"{name}: image is correct but the definition has no environment "
                    "variables — the service would boot without DATABASE_URL."
                )
            print(f"{name}: running {running} ({env_count} env vars)")
            return

        last = f"image={running!r} archive={bool(definition.get('archive'))}"
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(POLL_SECONDS)

    raise SystemExit(
        f"{name}: still not on {image} after {MAX_ATTEMPTS * POLL_SECONDS}s — last saw {last}. "
        "Production is running a different build than this commit."
    )


def main() -> int:
    token = os.environ["KOYEB_TOKEN"]
    image = f"{os.environ['IMAGE']}:sha-{os.environ['GITHUB_SHA']}"
    for var in ("API_SERVICE_ID", "WORKER_SERVICE_ID"):
        confirm(os.environ[var], image, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
