"""Early-access signups from the landing page.

Deliberately *not* tenant-scoped: these are prospects, captured before any
tenant exists. The table is still behind FORCE RLS with an insert-only policy,
so the application can record a signup but can never read the list back — a
bug or an injection in a public, unauthenticated endpoint therefore cannot
enumerate who has signed up. Reading the list is an admin-role job.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKey


class EarlyAccessSignup(Base, UUIDPrimaryKey):
    __tablename__ = "early_access_signups"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="landing")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
