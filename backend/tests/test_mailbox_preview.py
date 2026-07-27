"""The look-before-you-import step (§6.2).

What these hold onto: the preview must never invent a number, the cap must be
real rather than advisory, and consent alone must not start ingesting anything.
That last one is the whole reason this step exists — the previous behaviour
imported ninety days the moment the user clicked "allow".
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import settings
from app.services.graph.client import GraphClient
from app.services.graph.preview import offered_windows, preview_inbox


def _graph(handler) -> GraphClient:
    return GraphClient("token", transport=httpx.MockTransport(handler))


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload), headers={"content-type": "application/json"}
    )


@pytest.mark.asyncio
async def test_preview_reports_graphs_own_counts():
    """Every number shown comes from Graph, per window."""
    counts = {7: 12, 30: 140, settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS: 3905}
    seen_filters = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            params = request.url.params
            if "$count" in params:
                assert request.headers["ConsistencyLevel"] == "eventual"
                since = params["$filter"].split(" ge ")[1]
                start = datetime.fromisoformat(since.replace("Z", "+00:00"))
                days = round((datetime.now(UTC) - start).total_seconds() / 86400)
                seen_filters.append(days)
                return _json({"@odata.count": counts[days], "value": []})
            return _json({"value": [{"receivedDateTime": "2024-01-05T09:00:00Z"}]})
        return _json({"displayName": "Inbox", "totalItemCount": 8123})

    async with _graph(handler) as client:
        preview = await preview_inbox(client, "oid-1")

    assert preview.total == 8123
    assert preview.oldest_received == datetime(2024, 1, 5, 9, tzinfo=UTC)
    by_key = {w.key: w.emails for w in preview.windows}
    assert by_key["now"] is None  # nothing historical to count
    assert by_key["7d"] == 12
    assert by_key["30d"] == 140
    assert sorted(seen_filters) == sorted(counts)


@pytest.mark.asyncio
async def test_a_window_graph_will_not_count_is_offered_without_a_number():
    """A refused `$count` must not fabricate one, and must not block connecting.

    `$count` support varies by account type. Guessing here would put a wrong
    figure in front of a decision the user makes based on that figure.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "$count" in request.url.params:
            return _json({"error": "unsupported"}, status=400)
        if request.url.path.endswith("/messages"):
            return _json({"value": []})
        return _json({"displayName": "Inbox", "totalItemCount": 4})

    async with _graph(handler) as client:
        preview = await preview_inbox(client, "oid-1")

    assert preview.oldest_received is None
    assert [w.emails for w in preview.windows] == [None] * len(preview.windows)


def test_no_window_exceeds_what_the_backfill_can_honour():
    """The lookback cap is enforced here, not just documented.

    Offering a period the backfill will silently truncate is worse than not
    offering it: the user believes they imported a year.
    """
    cap = settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS
    assert all(days is None or days <= cap for _key, _label, days in offered_windows())


def test_from_now_on_is_offered_first():
    """The zero-import option leads, because it is the cheapest to undo."""
    assert offered_windows()[0][0] == "now"


def test_consent_alone_provisions_nothing():
    """Approving the scope must not start an import (§6.2).

    This is the regression the whole step exists for: consent used to create a
    mailbox, a subscription, and a 90-day backfill in one redirect, with the
    period chosen for the user — who never saw the choice being made.

    Asserted on the callable rather than through a request because there is no
    observable output to check: the point is precisely that nothing happens.
    """
    from app.api import auth

    called = auth._store_mailbox_consent.__code__.co_names
    assert "_provision_mailbox" not in called
    assert "enqueue" not in called
    # And the whole module: consent is the only path that ran these, so nothing
    # in `auth` should still be able to queue ingestion work.
    assert not hasattr(auth, "enqueue")


def test_window_keys_resolve_to_the_dates_they_promise():
    """"Last 7 days" must mean seven days, and "from now on" must mean no history."""
    from app.api.mailbox import _resolve_window

    now = datetime.now(UTC)
    assert abs((_resolve_window("now") - now).total_seconds()) < 5
    assert abs((_resolve_window("7d") - (now - timedelta(days=7))).total_seconds()) < 5


def test_an_unoffered_period_is_refused():
    """The cap has to be real: a client cannot ask for ten years by hand."""
    from fastapi import HTTPException

    from app.api.mailbox import _resolve_window

    with pytest.raises(HTTPException) as exc:
        _resolve_window("3650d")
    assert exc.value.status_code == 400
