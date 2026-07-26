#!/usr/bin/env python3
"""Point Koyeb services at a freshly built image.

Reads the service's *current* deployment definition and swaps only the source,
rather than PATCHing a bare `{"definition": {"docker": ...}}`. A partial
definition risks dropping env vars, ports and scaling — these services carry
nine environment variables, and losing them takes production down.

This also converts a service from archive source to docker source on first
run, so no manual migration step is needed and re-running is idempotent.

Standard library only: the deploy job holds production credentials, so it
installs nothing.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://app.koyeb.com/v1"


def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"Koyeb {method} {path} failed: HTTP {e.code}\n{detail}") from e


SOURCE_KEYS = ("archive", "git", "docker")
RUN_KEYS = ("command", "args", "entrypoint", "privileged")
# A deployment in one of these states is not a definition worth copying.
FAILED_STATUSES = frozenset({"ERROR", "ERRORING", "CANCELED", "CANCELING", "STASHED", "DEGRADED"})


def build_payload(definition: dict, image: str, registry_secret: str) -> dict:
    """Return `definition` with its source swapped to a docker image.

    Pure so it can be tested without touching Koyeb.

    The run configuration (command/args/entrypoint) lives *inside* the source
    block, so an archive→docker conversion has to carry it across: the
    worker's `python -u -m app.workers.main` is defined there, and losing it
    would silently start a second copy of the API.
    """
    definition = dict(definition)

    # Collect run config from whichever source block is present, then drop all
    # of them. Popping with `or` would short-circuit and leave a second source
    # key in the payload.
    carried: dict = {}
    for key in SOURCE_KEYS:
        block = definition.pop(key, None) or {}
        inner = (block.get("docker") if key != "docker" else block) or {}
        for run_key in RUN_KEYS:
            value = inner.get(run_key)
            # Explicitly skip empty values: the API emits "" / [] for unset
            # fields, and letting those through would blank the real command.
            if run_key not in carried and value not in (None, "", [], False):
                carried[run_key] = value

    definition["docker"] = {
        **carried,
        "image": image,
        "image_registry_secret": registry_secret,
    }
    # `type` is the SERVICE type (WEB / WORKER), not a source discriminator —
    # setting it to "DOCKER" was wrong and Koyeb silently ignored it. The
    # source is decided by which of archive/git/docker is populated. Explicit
    # nulls matter: PATCH merges, so simply omitting `archive` leaves the old
    # archive source in place and the deploy no-ops while reporting success.
    definition["archive"] = None
    definition["git"] = None
    return definition


def deploy(service_id: str, image: str, registry_secret: str, token: str) -> tuple[str, dict]:
    """Build the payload for one service. Does not send it — see main()."""
    service = _request("GET", f"/services/{service_id}", token)["service"]

    # Prefer the latest deployment: one still provisioning is the intended
    # state, and copying `active` would silently revert it. But a latest that
    # FAILED is a config someone rolled back from — redeploying it would
    # reinstate the breakage, so fall back to whatever is actually running.
    deployment_id = service.get("latest_deployment_id") or service["active_deployment_id"]
    deployment = _request("GET", f"/deployments/{deployment_id}", token)["deployment"]
    if deployment.get("status") in FAILED_STATUSES:
        fallback = service.get("active_deployment_id")
        if fallback and fallback != deployment_id:
            deployment = _request("GET", f"/deployments/{fallback}", token)["deployment"]
    current = deployment["definition"]

    definition = build_payload(current, image, registry_secret)

    if not definition.get("env"):
        raise SystemExit(
            f"{service['name']}: refusing to deploy a definition with no environment "
            "variables — the service would boot without DATABASE_URL."
        )
    return service["name"], definition


def main() -> int:
    token = os.environ["KOYEB_TOKEN"]
    image = f"{os.environ['IMAGE']}:sha-{os.environ['GITHUB_SHA']}"
    registry_secret = os.environ["KOYEB_REGISTRY_SECRET"]
    service_ids = [os.environ["API_SERVICE_ID"], os.environ["WORKER_SERVICE_ID"]]

    # Build every payload before sending any. If the worker's definition is
    # malformed we fail having changed nothing, rather than leaving the API on
    # the new image and the worker on the old one.
    planned = [deploy(sid, image, registry_secret, token) for sid in service_ids]

    for service_id, (name, definition) in zip(service_ids, planned, strict=True):
        _request("PATCH", f"/services/{service_id}", token, {"definition": definition})
        command = definition["docker"].get("command") or "(image default)"
        print(f"{name}: -> {image} | cmd={command} | {len(definition['env'])} env vars preserved")

        # Read back, because PATCH merges and has silently kept the old source
        # before — the deploy reported success while Koyeb went on rebuilding
        # from the previous archive. Assert the change actually landed.
        service = _request("GET", f"/services/{service_id}", token)["service"]
        applied = _request(
            "GET", f"/deployments/{service['latest_deployment_id']}", token
        )["deployment"]["definition"]
        got = (applied.get("docker") or {}).get("image")
        if applied.get("archive") or applied.get("git") or got != image:
            raise SystemExit(
                f"{name}: deploy did not take. Expected docker image {image}, "
                f"got image={got!r} archive={bool(applied.get('archive'))} "
                f"git={bool(applied.get('git'))}."
            )
        print(f"{name}: verified running {got}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
