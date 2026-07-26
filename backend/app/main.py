"""expressautomate.app API entrypoint."""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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

api.include_router(auth.router)


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
