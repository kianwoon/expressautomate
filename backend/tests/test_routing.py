"""API paths live under /api, and the static site owns everything else.

One Koyeb service serves both, on a single "/" route, so nothing strips a
prefix: `/api/health` arrives exactly as written. (An earlier split put the
API behind a `/api` Koyeb route that stripped the prefix, which is why
API_ROOT_PATH exists — it is empty now.)
"""

from app.core.config import settings
from app.main import app


def _declared_paths() -> set[str]:
    """Public paths as the app actually advertises them.

    Read from the OpenAPI schema rather than walking `app.routes`: routes added
    via `include_router` appear there as a nested object with no `.path`, so
    walking the list silently returned nothing and the assertions passed
    vacuously.
    """
    return set(app.openapi()["paths"])


def test_every_api_route_is_under_the_api_prefix() -> None:
    """Anything outside /api collides with the static site mounted at "/"."""
    strays = sorted(p for p in _declared_paths() if not p.startswith("/api"))
    assert strays == [], f"routes outside /api are shadowed by the site mount: {strays}"


def test_expected_endpoints_exist() -> None:
    paths = _declared_paths()
    for expected in ("/api/early-access", "/api/health", "/api/health/db"):
        assert expected in paths, f"{expected} missing — the public URL would break"


def test_root_path_is_empty() -> None:
    """Nothing proxies in front of this service, so no prefix is stripped."""
    assert settings.API_ROOT_PATH == ""
    assert app.root_path == ""


def test_the_app_answers_the_public_api_path() -> None:
    from fastapi.testclient import TestClient

    # No `with`: entering TestClient runs the lifespan, which needs a database.
    assert TestClient(app).get("/api/health").status_code == 200


def test_root_path_is_normalised() -> None:
    """Kept because a stray slash silently breaks /docs if the prefix returns."""
    from app.core.config import Settings

    def root_path_for(raw: str) -> str:
        return Settings(
            _env_file=None,
            APP_SECRET_KEY="x",
            TOKEN_ENCRYPTION_KEY="x",
            FRONTEND_ORIGIN="http://localhost:3000",
            DATABASE_URL="postgresql://u:p@localhost:5432/db",
            API_ROOT_PATH=raw,
        ).API_ROOT_PATH

    assert root_path_for("/api/") == "/api"
    assert root_path_for("//api") == "/api"
    assert root_path_for("") == ""
