"""Who is in this agency.

Not paginated: the vertical is agencies of 3-50 recruiters, and a picker that
pages is a picker that hides the person you want.

Any authenticated member may call it — an agency's own staff list is not a
secret from its own staff. RLS scopes the read to the caller's tenant, the
same as every other read in this codebase.
"""

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.auth import _require_session
from app.db.rls import tenant_session
from app.models import User

router = APIRouter(tags=["members"])


@router.get("/members")
async def list_members(request: Request) -> list[dict]:
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        rows = (
            await session.execute(
                select(User.id, User.preferred_name, User.display_name, User.email, User.role)
            )
        ).all()

    return sorted(
        (
            {
                "id": str(row.id),
                # preferred_name first: `app/models/tenant.py` says it takes
                # priority everywhere a name is shown. The email local-part is
                # the last resort so no row renders blank.
                "name": (
                    (row.preferred_name or "").strip()
                    or (row.display_name or "").strip()
                    or row.email.split("@")[0]
                ),
                "email": row.email,
                "role": row.role,
            }
            for row in rows
        ),
        key=lambda m: m["name"].casefold(),
    )
