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


def deploy(service_id: str, image: str, registry_secret: str, token: str) -> None:
    service = _request("GET", f"/services/{service_id}", token)["service"]
    deployment_id = service.get("active_deployment_id") or service["latest_deployment_id"]
    definition = _request("GET", f"/deployments/{deployment_id}", token)["deployment"][
        "definition"
    ]

    # Swap the source, preserving everything else verbatim.
    #
    # The command/args live INSIDE the source block, so converting from archive
    # to docker has to carry them across — the worker's
    # `python -u -m app.workers.main` is defined there, and dropping it would
    # silently start a second copy of the API instead.
    previous_source = definition.pop("archive", None) or definition.pop("git", None) or {}
    carried = {
        key: value
        for key, value in (previous_source.get("docker") or {}).items()
        if key in ("command", "args", "entrypoint", "privileged") and value not in (None, "", [])
    }
    definition["docker"] = {
        **carried,
        **definition.get("docker", {}),
        "image": image,
        "image_registry_secret": registry_secret,
    }

    env_count = len(definition.get("env", []))
    if not env_count:
        raise SystemExit(
            f"{service['name']}: refusing to deploy a definition with no environment "
            "variables — the service would boot without DATABASE_URL."
        )

    _request("PATCH", f"/services/{service_id}", token, {"definition": definition})
    command = definition["docker"].get("command") or "(image default)"
    print(f"{service['name']}: -> {image} | cmd={command} | {env_count} env vars preserved")


def main() -> int:
    token = os.environ["KOYEB_TOKEN"]
    image = f"{os.environ['IMAGE']}:sha-{os.environ['GITHUB_SHA']}"
    registry_secret = os.environ["KOYEB_REGISTRY_SECRET"]

    for var in ("API_SERVICE_ID", "WORKER_SERVICE_ID"):
        deploy(os.environ[var], image, registry_secret, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
