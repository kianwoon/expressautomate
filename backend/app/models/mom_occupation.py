"""MOM Resident Occupational Wages — the salary benchmark reference library.

Global reference data, not tenant-scoped. This is the first table in the schema
that every agency reads identically, so it deliberately omits `tenant_id` and
instead of the `tenant_isolation` RLS policy it carries a permissive
`USING (true)` SELECT policy — `verify_rls_enforced()` still demands FORCE ROW
LEVEL SECURITY on every readable table, and a global reference table satisfies
that demand by forcing RLS on and then admitting every row through the policy.

Writes happen only from the one-off seed script (`scripts/seed_mom_occupations.py`)
running under the admin role (BYPASSRLS), never from a request or a tenant
session — the table has no INSERT/UPDATE/DELETE policy, and the application role
has no DML grant, so a tenant session cannot mutate it even by mistake.

The wage columns are stored as `Numeric` rather than `float` to preserve the
cents the survey reports; `Decimal` round-trips into JSON and renders without
floating-point drift. `embedding` is the `text-embedding-3-small` vector of the
occupation title, used by the Job Intelligence occupation-matching stage for
semantic search against an extracted work profile.
"""

from decimal import Decimal

from sqlalchemy import Integer, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base, UUIDPrimaryKey
from app.models.vector_type import Vector


class MomOccupation(Base, UUIDPrimaryKey):
    """One row of the MOM Resident Occupational Wages (June 2024) survey."""

    __tablename__ = "mom_occupations"

    # The survey year the wage figures were published for. A future release
    # (June 2025) becomes new rows under a different year rather than an
    # overwrite, so a recruiter can be told which vintage the benchmark is.
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Lower-cased MOM occupation description, e.g. "software developer". Unique
    # within a year — the survey emits one row per occupation per vintage.
    title: Mapped[str] = mapped_column(Text, nullable=False)

    # Monthly gross wage percentiles, the figures the benchmark card plots.
    gross_p25: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    gross_p50: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    gross_p75: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Monthly basic wage percentiles — stored for completeness and a future
    # "basic vs gross" breakdown, but not plotted in the v1 chart.
    basic_p25: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    basic_p50: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    basic_p75: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # `vector(1536)` matches text-embedding-3-small; set by the seed script, so
    # it is nullable until the backfill has run (a row without an embedding is
    # unsearchable but still readable for a future keyword fallback).
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=True
    )

    __table_args__ = (
        # One row per occupation per vintage, so a re-seed of the same CSV is
        # an upsert rather than a duplicate.
        UniqueConstraint("year", "title", name="uq_mom_occupations_year_title"),
    )
