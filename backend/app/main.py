"""expressautomate.app API entrypoint."""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import insert, text

from app.api import auth
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.rls import verify_rls_enforced
from app.db.session import SessionLocal, engine
from app.models import EarlyAccessSignup

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
    )
    yield
    await engine.dispose()
    log.info("shutdown")


app = FastAPI(
    title="expressautomate.app",
    description="AI recruitment intelligence & operations platform",
    version="0.1.0",
    lifespan=lifespan,
    # Public prefix stripped by Koyeb's router — see settings.API_ROOT_PATH.
    root_path=settings.API_ROOT_PATH,
    # A 307 from the slash-redirect is built from the *stripped* path, so
    # `POST /api/early-access/` would redirect to `/early-access` — off this
    # service entirely and onto the landing page. Better a clean 404.
    redirect_slashes=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)


class EarlyAccessRequest(BaseModel):
    email: EmailStr
    source: str = Field(default="landing", max_length=64)


@app.post("/early-access", status_code=201)
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}


@app.get("/health/db")
async def health_db() -> dict[str, str]:
    async with engine.connect() as conn:
        db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    return {"status": "ok", "database": db}

