"""expressautomate.app API entrypoint."""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import insert, text

from app.api import (
    activity,
    auth,
    candidate_documents,
    candidate_imports,
    candidate_ownership,
    candidate_roles,
    candidate_shares,
    candidate_whatsapp,
    candidates,
    candidates_avatar,
    clients,
    clients_logo,
    events,
    glossary,
    graph_webhook,
    mailbox,
    members,
    notifications,
    opportunities,
    opportunity_shares,
    sourcing,
    telegram_webhook,
    wa_gateway,
    whatsapp_webhook,
)
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.rls import verify_rls_enforced
from app.db.session import SessionLocal, engine
from app.models import EarlyAccessSignup
from app.services.events import close_client
from app.services.graph.client import warn_if_unconfigured
from app.services.storage.r2 import BodyStoreMisconfigured

# allow-hardcode: user-facing copy, not matching logic. Deliberately says
# nothing about *what* is misconfigured — the bucket name and endpoint live in
# the exception message, which is logged and never serialised to a client.
_STORAGE_UNAVAILABLE_DETAIL = "Photo storage is unavailable."

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Refuse to serve if tenant isolation is not actually being enforced —
    # a silent boot here would mean every query reads every agency's data.
    await verify_rls_enforced()
    log.info(
        "startup",
        env=settings.APP_ENV,
        microsoft_configured=settings.microsoft_configured(),
        google_configured=settings.google_configured(),
        graph_configured=settings.graph_configured(),
    )
    if settings.microsoft_configured():
        warn_if_unconfigured("api")
    yield
    # Before the engine, so an open dashboard stream unwinds while its Redis
    # client still exists rather than logging a closed-transport error on the
    # way out.
    await close_client()
    await engine.dispose()
    log.info("shutdown")


app = FastAPI(
    title="expressautomate.app",
    description="AI recruitment intelligence & operations platform",
    version="0.1.0",
    lifespan=lifespan,
    # One service serves both the site and the API on a single Koyeb route
    # ("/"), so nothing strips a prefix any more — API paths carry /api
    # literally, via `api` below.
    root_path=settings.API_ROOT_PATH,
    redirect_slashes=False,
)

# Everything under /api. The static site owns everything else.
api = APIRouter(prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(BodyStoreMisconfigured)
async def _body_store_misconfigured(
    request: Request, exc: BodyStoreMisconfigured
) -> JSONResponse:
    """Object storage the operator has to fix: 503, and say nothing else.

    Registered on the app rather than in one router because this is not an
    avatar concern — any endpoint that touches the body store can raise it.

    503 rather than 500 because the condition is real and external: the
    request was well formed and will work once someone sets the variables.
    The exception message names the bucket and the endpoint, which is exactly
    what the operator needs and exactly what a client must never be told, so
    it goes to the log and the response carries fixed copy instead.
    """
    log.error(
        "body_store_misconfigured",
        path=request.url.path,
        error=str(exc),
        cause=str(exc.__cause__) if exc.__cause__ else None,
    )
    return JSONResponse(status_code=503, content={"detail": _STORAGE_UNAVAILABLE_DETAIL})


api.include_router(auth.router)
api.include_router(mailbox.router)
api.include_router(graph_webhook.router)
# Before `opportunities` and before `candidates`, deliberately, and for the
# reason `candidate_imports` is below: a literal segment declared after a
# `{param}` route on the same prefix is never reached. `opportunities.router`
# owns `/opportunities/{opportunity_id}/review` today and `candidates.router`
# owns `/candidates/{candidate_id}`, so `/candidates/{id}/submissions` in
# particular would be shadowed the moment `candidates` grew a matching
# `{candidate_id}/{something}` route. Declaring the narrower paths first makes
# that ordering explicit rather than accidental; `test_sourcing_api.py` asserts
# each of these paths still resolves.
api.include_router(sourcing.router)
api.include_router(opportunities.router)
api.include_router(opportunity_shares.router)
api.include_router(activity.router)
api.include_router(glossary.router)
api.include_router(members.router)
# Before `candidates`, deliberately: `/candidates/imports` would otherwise be
# matched by `/candidates/{candidate_id}` and answered as a malformed UUID.
api.include_router(candidate_imports.router)
# Before `candidates`, for the reason the comment above gives: `candidates`
# owns `/candidates/{candidate_id}`, and `/candidates/{id}/whatsapp-send`
# would be shadowed if that generic route were declared first.
api.include_router(candidate_whatsapp.router)
# Before `candidates`, for the same reason: `/candidates/{id}/claim` and
# `/candidates/{id}/assign` are narrower than anything `candidates` owns
# today, and declaring them first keeps them so if it ever grows a
# `{candidate_id}/{something}` route.
api.include_router(candidate_ownership.router)
# Before `candidates`, and this one is not merely tidy: `candidate_shares`
# owns the LITERAL path `/candidates/access-requests`. Declared after
# `/candidates/{candidate_id}`, that literal is never reached — FastAPI hands
# `access-requests` to the generic route as a path parameter and answers 422
# "not a valid UUID", which reads as a broken inbox rather than a routing
# order. `tests/test_candidate_shares_api.py` asserts the inbox resolves.
api.include_router(candidate_shares.router)
api.include_router(candidates.router)
api.include_router(candidate_roles.router)
api.include_router(candidates_avatar.router)
api.include_router(candidate_documents.router)
api.include_router(clients.router)
api.include_router(clients_logo.router)
api.include_router(notifications.router)
api.include_router(events.router)
api.include_router(telegram_webhook.router)
api.include_router(whatsapp_webhook.router)
api.include_router(wa_gateway.router)


class EarlyAccessRequest(BaseModel):
    email: EmailStr
    source: str = Field(default="landing", max_length=64)


@api.post("/early-access", status_code=201)
async def early_access(payload: EarlyAccessRequest) -> dict[str, str]:
    """Record a landing-page signup.

    Public and unauthenticated. The table is insert-only under RLS, so this
    endpoint cannot read back who has signed up. Duplicate submissions are
    accepted rather than reported — telling an anonymous caller whether an
    address is already on the list would leak it.
    """
    # A Core INSERT with no RETURNING clause, deliberately. The ORM would add
    # RETURNING to fetch server defaults, and Postgres evaluates RETURNING
    # against the SELECT policy — which this table does not have, by design.
    # The id is generated here so nothing needs reading back.
    async with SessionLocal() as session:
        await session.execute(
            insert(EarlyAccessSignup).values(
                id=uuid.uuid4(), email=str(payload.email), source=payload.source
            )
        )
        await session.commit()
    log.info("early_access_signup", source=payload.source)
    return {"status": "received"}


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}


@api.get("/health/db")
async def health_db() -> dict[str, str]:
    async with engine.connect() as conn:
        db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    return {"status": "ok", "database": db}


app.include_router(api)

# Mounted last: StaticFiles(html=True) claims "/" and would swallow any route
# declared after it. The Next.js static export is copied here at image build.
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="site")
