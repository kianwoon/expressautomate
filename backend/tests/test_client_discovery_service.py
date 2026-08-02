"""Client auto-discovery: the scan, the exclusions, the ranking.

What these hold onto: the scan is **headers only** — every request's `$select`
is asserted, because "no bodies" is the constraint the whole feature was
approved under; the exclusion gates are configuration, not literals; and every
number ranked comes from the observed mail, weighted by settings.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import settings
from app.services import client_discovery as svc
from app.services.graph.client import GraphClient


def _graph(handler) -> GraphClient:
    return GraphClient("token", transport=httpx.MockTransport(handler))


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload), headers={"content-type": "application/json"}
    )


NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=90)


def _inbox_message(address: str, name: str | None, when: str) -> dict:
    return {
        "from": {"emailAddress": {"address": address, "name": name}},
        "receivedDateTime": when,
    }


def _sent_message(recipients: list[tuple[str, str | None]], when: str) -> dict:
    return {
        "toRecipients": [
            {"emailAddress": {"address": a, "name": n}} for a, n in recipients
        ],
        "sentDateTime": when,
    }


# ---------------------------------------------------------------------------
# Exclusion gates
# ---------------------------------------------------------------------------


def test_free_providers_and_malformed_addresses_are_refused():
    own = frozenset({"myagency.sg"})
    assert svc.usable_domain("boss@gmail.com", own) is None
    assert svc.usable_domain("not-an-address", own) is None
    assert svc.usable_domain(None, own) is None
    assert svc.usable_domain("hr@acmecorp.com.sg", own) == "acmecorp.com.sg"


def test_the_recruiters_own_domain_is_not_a_client():
    own = frozenset({"myagency.sg"})
    assert svc.usable_domain("colleague@myagency.sg", own) is None
    assert svc.usable_domain("colleague@mail.myagency.sg", own) is None


def test_excluded_domains_match_by_suffix(monkeypatch):
    monkeypatch.setattr(
        settings, "CLIENT_DISCOVERY_EXCLUDED_DOMAINS_RAW", "linkedin.com"
    )
    own = frozenset()
    assert svc.usable_domain("jobs@linkedin.com", own) is None
    assert svc.usable_domain("x@bounce.linkedin.com", own) is None
    # A name that merely contains the entry is not a subdomain of it.
    assert svc.usable_domain("x@notlinkedin.com", own) == "notlinkedin.com"


def test_system_localparts_are_machinery_not_people(monkeypatch):
    monkeypatch.setattr(
        settings, "CLIENT_DISCOVERY_SYSTEM_LOCALPARTS_RAW", "noreply,alert"
    )
    own = frozenset()
    assert svc.usable_domain("noreply@acme.com", own) is None
    assert svc.usable_domain("noreply1@acme.com", own) is None  # digit boundary
    assert svc.usable_domain("noreply+tag@acme.com", own) is None
    assert svc.usable_domain("alert-team@acme.com", own) is None
    # A prefix followed by a letter is a longer word — a person, not a system.
    assert svc.usable_domain("alertan@acme.com", own) == "acme.com"


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_is_headers_only_and_aggregates_both_folders():
    """`$select` on every request names headers only — never a body."""
    selects: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "sentitems" in path.lower():
            selects["sent"] = request.url.params["$select"]
            assert "sentDateTime ge " in request.url.params["$filter"]
            return _json(
                {
                    "value": [
                        _sent_message(
                            [
                                ("jane.lim@acme.com.sg", "Jane Lim"),
                                ("bob.tan@acme.com.sg", "Bob Tan"),
                            ],
                            "2026-08-01T08:00:00Z",
                        ),
                        _sent_message([("hr@globex.com", None)], "2026-07-01T08:00:00Z"),
                    ]
                }
            )
        selects["inbox"] = request.url.params["$select"]
        assert "receivedDateTime ge " in request.url.params["$filter"]
        return _json(
            {
                "value": [
                    _inbox_message("jane.lim@acme.com.sg", "Jane Lim", "2026-07-30T10:00:00Z"),
                    _inbox_message("jane.lim@acme.com.sg", "Jane Lim", "2026-07-20T10:00:00Z"),
                    _inbox_message("noreply@acme.com.sg", None, "2026-07-21T10:00:00Z"),
                    _inbox_message("boss@gmail.com", "Boss", "2026-07-22T10:00:00Z"),
                ]
            }
        )

    async with _graph(handler) as client:
        result = await svc.scan_headers(client, since=SINCE, own_domains=frozenset())

    assert selects == {
        "inbox": "from,receivedDateTime",
        "sent": "toRecipients,sentDateTime",
    }
    assert result.inbox_scanned == 4
    assert result.sent_scanned == 2
    assert not result.truncated

    acme = result.domains["acme.com.sg"]
    assert acme.received == 2
    # One message to two people at acme is ONE interaction with acme...
    assert acme.sent == 1
    # ...but both people are contacts.
    assert set(acme.contacts) == {"jane.lim@acme.com.sg", "bob.tan@acme.com.sg"}
    assert acme.contacts["jane.lim@acme.com.sg"].inbound == 2
    assert acme.contacts["jane.lim@acme.com.sg"].outbound == 1
    assert acme.last_activity == datetime(2026, 8, 1, 8, tzinfo=UTC)

    globex = result.domains["globex.com"]
    assert globex.received == 0 and globex.sent == 1
    # No display name on file: the address itself, never an invented name.
    assert globex.contacts["hr@globex.com"].name == "hr@globex.com"

    # The machinery and free-provider senders never became domains.
    assert set(result.domains) == {"acme.com.sg", "globex.com"}


@pytest.mark.asyncio
async def test_scan_follows_next_links_and_stops_at_the_budget(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_MAX_MESSAGES", 2)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "page2" in str(request.url):
            return _json(
                {
                    "value": [_inbox_message("c@corp2.com", "C", "2026-07-03T10:00:00Z")],
                    "@odata.nextLink": "https://graph.example/page3",
                }
            )
        return _json(
            {
                "value": [
                    _inbox_message("a@corp1.com", "A", "2026-07-01T10:00:00Z"),
                    _inbox_message("b@corp1.com", "B", "2026-07-02T10:00:00Z"),
                ],
                "@odata.nextLink": "https://graph.example/page2",
            }
        )

    async with _graph(handler) as client:
        result = await svc.scan_headers(client, since=SINCE, own_domains=frozenset())

    # Whole pages, budget between pages: the first page filled the budget, so
    # the walk stopped there — page2 was never fetched, and Sent Items was
    # never opened. That is a truncated scan and says so.
    assert result.inbox_scanned == 2
    assert result.sent_scanned == 0
    assert result.truncated
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _domain_seen(received=0, sent=0, contacts=(), last=None) -> svc.DomainSeen:
    seen = svc.DomainSeen(domain="acme.com", received=received, sent=sent, last_activity=last)
    for email in contacts:
        seen.contacts[email] = svc.ContactSeen(email=email, name=email)
    return seen


def test_score_reads_its_weights_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_WEIGHT_RECEIVED", 1.0)
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_WEIGHT_SENT", 2.0)
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_WEIGHT_UNIQUE_CONTACTS", 5.0)
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_RECENCY_BONUS", 10.0)
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_RECENCY_DAYS", 14)

    stale = _domain_seen(
        received=3, sent=2, contacts=["a@x.com"], last=NOW - timedelta(days=30)
    )
    assert svc.score(stale, now=NOW) == 3 * 1.0 + 2 * 2.0 + 1 * 5.0

    fresh = _domain_seen(
        received=3, sent=2, contacts=["a@x.com"], last=NOW - timedelta(days=3)
    )
    assert svc.score(fresh, now=NOW) == 3 * 1.0 + 2 * 2.0 + 1 * 5.0 + 10.0


def test_entries_rank_domains_and_cap_contacts(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_DISCOVERY_MAX_CONTACTS_PER_CLIENT", 2)

    busy = svc.DomainSeen(domain="busy.com", received=10)
    for email, volume in (("a@busy.com", 1), ("b@busy.com", 5), ("c@busy.com", 3)):
        busy.contacts[email] = svc.ContactSeen(email=email, name=email, inbound=volume)
    quiet = svc.DomainSeen(domain="quiet.com", received=1)

    entries = svc.ranked_entries(
        {"busy.com": busy, "quiet.com": quiet}, now=NOW
    )
    assert [e["domain"] for e in entries] == ["busy.com", "quiet.com"]
    # Capped to the two most active, most active first.
    assert [c["email"] for c in entries[0]["contacts"]] == ["b@busy.com", "c@busy.com"]
    assert entries[0]["unique_contacts"] == 3  # the count is the truth, uncapped
    assert entries[0]["created"] is False


def test_a_placeholder_name_upgrades_when_a_real_one_arrives():
    seen = svc.DomainSeen(domain="acme.com")
    svc._note_contact(seen, "Jane@acme.com", None, inbound=True, when=None)
    assert seen.contacts["jane@acme.com"].name == "jane@acme.com"
    svc._note_contact(seen, "jane@acme.com", "Jane Lim", inbound=True, when=None)
    assert seen.contacts["jane@acme.com"].name == "Jane Lim"
    # A real name, once seen, stays stable.
    svc._note_contact(seen, "jane@acme.com", "J. Lim", inbound=False, when=None)
    assert seen.contacts["jane@acme.com"].name == "Jane Lim"
