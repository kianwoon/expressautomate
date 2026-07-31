"""The route table, written down where the browser's tests can read it.

Two bugs shipped because nothing checked that a URL the frontend builds is a
URL this app serves: `PATCH /api/opportunities/{id}` and `GET
/api/opportunities/{id}` were both called from the dashboard and neither
existed. Every frontend test passed, because a stubbed `fetch` answers any URL
with a plausible body — a call to nothing is indistinguishable from a call
that worked.

So the two halves are joined here. This test regenerates the manifest from
`app.routes` and fails if the checked-in copy has drifted; `frontend/app/
api.contract.test.ts` reads the same file and fails if a path helper resolves
to a template that is not in it. Neither needs a server running, and the file
is checked in so the frontend's tests do not need Python.

Regenerate with `uv run pytest tests/test_route_manifest.py --regenerate` or
by hand: the failure below prints what changed.
"""

import json
import pathlib

from app.main import app

MANIFEST = (
    pathlib.Path(__file__).resolve().parents[2] / "frontend" / "route-manifest.json"
)


def build() -> dict:
    """Every path template the app serves, with the methods it serves on it.

    Read from the OpenAPI schema rather than by walking `app.routes`, for the
    reason `test_routing.py` gives: a router included with `include_router`
    appears in `app.routes` as one object with no `.path`, so the walk quietly
    returns nothing and every assertion passes over an empty set.
    """
    schema = app.openapi()["paths"]
    return {
        "paths": {
            path: sorted(method.upper() for method in operations)
            for path, operations in sorted(schema.items())
            if path.startswith("/api")
        }
    }


def test_the_checked_in_manifest_matches_the_app(pytestconfig) -> None:
    current = build()
    if pytestconfig.getoption("--regenerate", default=False):
        MANIFEST.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    assert MANIFEST.exists(), f"{MANIFEST} is missing — regenerate it"
    recorded = json.loads(MANIFEST.read_text())

    added = sorted(set(current["paths"]) - set(recorded["paths"]))
    removed = sorted(set(recorded["paths"]) - set(current["paths"]))
    changed = sorted(
        path
        for path in set(current["paths"]) & set(recorded["paths"])
        if current["paths"][path] != recorded["paths"][path]
    )
    assert (added, removed, changed) == ([], [], []), (
        "frontend/route-manifest.json is stale. Rerun with --regenerate. "
        f"added={added} removed={removed} methods changed={changed}"
    )


def test_the_manifest_is_not_empty() -> None:
    """A generator that silently produced nothing would make the frontend's
    contract test pass over an empty table, which is the failure mode that
    whole test exists to prevent."""
    paths = build()["paths"]
    assert len(paths) > 20, paths
    assert "/api/opportunities/{opportunity_id}" in paths
