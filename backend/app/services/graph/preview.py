"""What is in this mailbox, before we read any of it (plan §6.2).

Consent has to come first — counting a mailbox needs a token — but consent is
not permission to import three months of someone's mail. This is the step in
between: look, report, and let the person decide.

The numbers matter more than they look. "Last 90 days" means nothing until it
says **3,905 emails** next to it; that is the difference between an informed
choice and a default someone regrets. Every figure here is Graph's own count,
never an estimate, and when Graph will not give one the option is offered
without a number rather than with a guess.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.graph.client import GraphClient, GraphError

log = get_logger(__name__)

# Graph answers `$count` on a filtered collection only with this header.
_COUNTABLE = {"ConsistencyLevel": "eventual"}

INBOX = "inbox"


@dataclass(frozen=True)
class Window:
    """One thing the user can choose to import."""

    key: str
    label: str
    days: int | None  # None means "from now on" — watch, import nothing.
    emails: int | None  # None when Graph declined to count.


@dataclass(frozen=True)
class InboxPreview:
    folder: str
    total: int | None
    oldest_received: datetime | None
    windows: list[Window]


def offered_windows() -> list[tuple[str, str, int | None]]:
    """The choices, capped by what the implementation can actually deliver.

    Nothing beyond `INITIAL_SYNC_MAX_LOOKBACK_DAYS` is offered: Graph delta
    filtered by date is not a bulk-export mechanism, and an option we cannot
    honour is worse than one we never showed.
    """
    cap = settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS
    choices: list[tuple[str, str, int | None]] = [
        ("now", "From now on", None),
        ("7d", "Last 7 days", 7),
        ("30d", "Last 30 days", 30),
    ]
    if cap > 30:
        choices.append((f"{cap}d", f"Last {cap} days", cap))
    return [c for c in choices if c[2] is None or c[2] <= cap]


async def preview_inbox(client: GraphClient, ms_user_id: str) -> InboxPreview:
    """Size the inbox and price each import option."""
    base = f"/users/{ms_user_id}/mailFolders/{INBOX}"

    folder = await client.get(base, params={"$select": "displayName,totalItemCount"})
    oldest = await _oldest_received(client, base)

    windows = []
    for key, label, days in offered_windows():
        emails = None if days is None else await _count_since(client, base, days)
        windows.append(Window(key=key, label=label, days=days, emails=emails))

    return InboxPreview(
        folder=folder.get("displayName") or "Inbox",
        total=folder.get("totalItemCount"),
        oldest_received=oldest,
        windows=windows,
    )


async def _oldest_received(client: GraphClient, base: str) -> datetime | None:
    """The date of the earliest message, for context on how far back it goes."""
    page = await client.get(
        f"{base}/messages",
        params={
            "$top": "1",
            "$orderby": "receivedDateTime asc",
            "$select": "receivedDateTime",
        },
    )
    items = page.get("value") or []
    if not items:
        return None
    raw = items[0].get("receivedDateTime")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


async def _count_since(client: GraphClient, base: str, days: int) -> int | None:
    """How many messages arrived in the last `days`.

    Returns None rather than raising if Graph declines — `$count` support
    varies, notably on personal accounts. An option shown without a number is
    honest; one shown with a fabricated number is not, and a failed count is
    no reason to block someone from connecting.

    `HTTPStatusError` is caught alongside `GraphError` because an account that
    does not support `$count` answers 400, which the client deliberately treats
    as "the request was wrong" rather than a modelled runtime state. Here it is
    a runtime state: this is the one caller that asks a question Graph is
    allowed to refuse.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    try:
        page = await client.get(
            f"{base}/messages",
            params={
                "$filter": f"receivedDateTime ge {since}",
                "$count": "true",
                "$top": "1",
                "$select": "id",
            },
            headers=_COUNTABLE,
        )
    except (GraphError, httpx.HTTPStatusError) as exc:
        log.info("inbox_count_unavailable", days=days, error=repr(exc))
        return None

    count = page.get("@odata.count")
    return int(count) if isinstance(count, int) else None
