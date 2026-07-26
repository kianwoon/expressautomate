"""The route-prefix contract with Koyeb's router.

Koyeb strips the matched route prefix before forwarding: a request to
`https://expressautomate.app/api/early-access` reaches this app as
`/early-access`. Declaring a route as `/api/early-access` therefore produces a
404 that looks like an application bug — which is exactly what shipped once.

Routes must stay unprefixed; `root_path` carries the public prefix so the
OpenAPI schema and /docs still emit correct URLs.
"""

from app.core.config import settings
from app.main import app

# Paths FastAPI adds for itself; not part of the contract.
_BUILTIN = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _declared_paths() -> set[str]:
    return {r.path for r in app.routes if getattr(r, "path", None)} - _BUILTIN


def test_no_route_is_declared_with_the_public_prefix() -> None:
    """A declared '/api/...' path is unreachable once Koyeb strips '/api'."""
    # Match the prefix as a path segment: a future '/api-keys' is legitimate.
    offenders = sorted(
        p for p in _declared_paths() if p == "/api" or p.startswith("/api/")
    )
    assert not offenders, (
        f"routes declared with the '/api' prefix will 404 behind Koyeb: {offenders}. "
        "Declare them unprefixed and let API_ROOT_PATH carry the public prefix."
    )


def test_expected_endpoints_exist_unprefixed() -> None:
    paths = _declared_paths()
    for expected in ("/early-access", "/health", "/health/db"):
        assert expected in paths, f"{expected} missing — public URL would break"


def test_root_path_is_configurable_and_empty_by_default() -> None:
    """Local runs have nothing in front, so the default must not add a prefix."""
    assert settings.API_ROOT_PATH == ""
    assert app.root_path == ""


def test_root_path_does_not_change_declared_paths() -> None:
    """root_path is a display concern; it must not be prepended to routes.

    If it were, the app would expect `/api/health` from Koyeb and 404 on the
    `/health` that actually arrives. (FastAPI only surfaces root_path in the
    served schema during a real request, so this asserts the routing side —
    the part that decides whether a URL resolves at all.)
    """
    from fastapi import FastAPI

    prefixed = FastAPI(root_path="/api")

    @prefixed.get("/health")
    async def _health() -> dict[str, str]:  # pragma: no cover - shape only
        return {"status": "ok"}

    assert prefixed.root_path == "/api"
    assert "/health" in prefixed.openapi()["paths"]
    assert "/api/health" not in prefixed.openapi()["paths"]


def test_a_prefix_stripped_request_resolves() -> None:
    """Reproduce production shape: proxy strips '/api', app still answers.

    This is the case that actually broke — the assertions above are about
    declaration, this one is about a request resolving.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    prefixed = FastAPI(root_path="/api")

    @prefixed.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(prefixed) as client:
        # What Koyeb forwards after stripping.
        assert client.get("/health").status_code == 200
        # What a caller types; TestClient applies root_path itself.
        assert client.get("/api/health").status_code == 200


def test_root_path_is_normalised() -> None:
    """A trailing slash silently breaks /docs; a missing leading slash too."""
    from app.core.config import Settings

    def root_path_for(raw: str) -> str:
        return Settings(
            APP_SECRET_KEY="x",
            TOKEN_ENCRYPTION_KEY="x",
            FRONTEND_ORIGIN="http://localhost:3000",
            DATABASE_URL="postgresql://u:p@localhost:5432/db",
            API_ROOT_PATH=raw,
        ).API_ROOT_PATH

    assert root_path_for("/api/") == "/api"
    assert root_path_for("api") == "/api"
    assert root_path_for("/api") == "/api"
    assert root_path_for("") == ""
    assert root_path_for("  ") == ""
