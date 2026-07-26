"""expressautomate.app API entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.rls import verify_rls_enforced
from app.db.session import engine

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
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}


@app.get("/health/db")
async def health_db() -> dict[str, str]:
    async with engine.connect() as conn:
        db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    return {"status": "ok", "database": db}
