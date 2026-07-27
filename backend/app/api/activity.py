"""What the pipeline has been doing, for the dashboard's activity panel.

One endpoint, read-only. It exists because the honest answer to "why has
nothing appeared today" has until now lived only in the workers' stdout, where
the person asking cannot reach it — and where a dead grant and a quiet inbox
produce the same silence on screen.

Deliberately not behind the mailbox-connected check `/mailbox/preview` uses:
this reads our own rows and touches Graph not at all, and the user whose grant
has just lapsed is exactly the one who needs to see the event saying so.
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session

router = APIRouter(tags=["activity"])

# allow-hardcode: a SQL statement, not a tunable.
#
# No tenant predicate in the WHERE clause, and that is not an omission: the row
# policy supplies it from the session setting. Repeating it here would let a
# future edit that drops the clause look harmless while the policy is what was
# really holding the line — and would hide a `tenant_session` that was never
# opened, since without one this returns zero rows rather than everything.
_RECENT = text(
    "SELECT kind, outcome, detail, created_at FROM sync_events"
    " ORDER BY created_at DESC, id DESC LIMIT :limit"
)


@router.get("/mailbox/activity")
async def sync_activity(request: Request) -> dict:
    """The most recent sync events for the signed-in user's agency, newest first.

    Scoped to the tenant rather than to the user's own mailbox: a small agency
    shares one tenant, and a recruiter noticing that a colleague's mailbox
    stopped syncing is a feature, not a leak — it is the same agency's data
    either way.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        rows = (
            await session.execute(
                _RECENT, {"limit": settings.SYNC_ACTIVITY_PAGE_LIMIT}
            )
        ).all()

    return {
        "events": [
            {
                "kind": row.kind,
                "outcome": row.outcome,
                "detail": row.detail,
                # ISO, not a formatted date: the timestamp is stored in UTC and
                # the recruiter reading it is in a browser that knows their
                # timezone. Formatting it here would freeze it to the server's.
                "at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        # Echoed so the panel can say "showing the latest 50" without hardcoding
        # a number the deployment owns.
        "limit": settings.SYNC_ACTIVITY_PAGE_LIMIT,
    }
