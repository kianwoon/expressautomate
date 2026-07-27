"""The Graph change-notification endpoints (plan §7, §8).

This is the first unauthenticated endpoint in the system, and it is reachable
by anyone who learns the URL. Two rules follow, and most of these tests exist
to hold one of them:

1. **Answer fast.** Graph allows roughly three seconds before it treats the
   notification as failed, so the handler does one insert and one enqueue and
   nothing else.
2. **Answer 2xx.** Graph retries non-2xx for hours. A notification we cannot
   act on — forged clientState, unknown subscription — is accepted and
   dropped, because retrying it would never start working.

allow-hardcode: the SQL below is test fixture data, not matching logic.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import graph_webhook
from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app

SUBSCRIPTION_ID = "sub-x"
CLIENT_STATE = "secret-x"


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


@pytest.fixture
async def enqueued(monkeypatch) -> list[tuple[str, dict]]:
    """Capture jobs instead of reaching Redis."""
    calls: list[tuple[str, dict]] = []

    async def _fake(name: str, **kwargs) -> bool:
        calls.append((name, kwargs))
        return True

    monkeypatch.setattr(graph_webhook, "enqueue", _fake)
    return calls


@pytest.fixture
async def subscription(admin_session):
    """A tenant, mailbox and active subscription, torn down by cascade."""
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at)"
            " VALUES (:id, :tenant, :mailbox, :sub, 'r', :state,"
            "         now() + interval '1 day')"
        ),
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "sub": SUBSCRIPTION_ID,
            "state": CLIENT_STATE,
        },
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _notification(message_id: str, *, state: str = CLIENT_STATE, sub: str = SUBSCRIPTION_ID):
    return {
        "value": [
            {
                "subscriptionId": sub,
                "clientState": state,
                "resourceData": {"id": message_id},
            }
        ]
    }


async def _rows(tenant_id: uuid.UUID) -> list:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT graph_message_id, processing_status, mailbox_id"
                    " FROM email_messages ORDER BY graph_message_id"
                )
            )
        ).all()


# --- the subscription handshake ---------------------------------------------


async def test_the_validation_token_comes_back_verbatim_as_text(client):
    """Graph sends this when a subscription is created and expects it echoed.

    Returning JSON fails the handshake, so the token is written straight to the
    body as text/plain rather than parsed and re-encoded.
    """
    token = "tok en+123/=&"

    response = await client.post(
        "/api/graph/notifications", params={"validationToken": token}
    )

    assert response.status_code == 200
    assert response.text == token
    assert response.headers["content-type"].startswith("text/plain")


async def test_the_handshake_needs_no_existing_subscription(client):
    """It arrives *before* the subscription exists — requiring one would make
    every subscription creation fail."""
    response = await client.post(
        "/api/graph/notifications", params={"validationToken": "abc"}
    )

    assert response.status_code == 200


async def test_the_lifecycle_endpoint_answers_the_handshake_too(client):
    """Graph validates both URLs at subscription creation."""
    response = await client.post("/api/graph/lifecycle", params={"validationToken": "xyz"})

    assert response.status_code == 200
    assert response.text == "xyz"


# --- notifications ----------------------------------------------------------


async def test_a_valid_notification_is_recorded_and_queued(client, subscription, enqueued):
    tenant_id, mailbox_id = subscription

    response = await client.post("/api/graph/notifications", json=_notification("MSG-1"))

    assert response.status_code == 202
    rows = await _rows(tenant_id)
    assert len(rows) == 1
    assert rows[0].graph_message_id == "MSG-1"
    assert rows[0].processing_status == "pending"
    assert rows[0].mailbox_id == mailbox_id

    assert len(enqueued) == 1
    name, kwargs = enqueued[0]
    assert name == "fetch_email"
    assert uuid.UUID(kwargs["email_message_id"])
    # The tenant travels with the job — background work has no session tenant,
    # and RLS validates the pair on the other end.
    assert kwargs["tenant_id"] == str(tenant_id)
    assert kwargs["mailbox_id"] == str(mailbox_id)


async def test_a_forged_client_state_stores_nothing(client, subscription, enqueued):
    """The comparison is the only thing protecting a public URL."""
    tenant_id, _ = subscription

    response = await client.post(
        "/api/graph/notifications", json=_notification("MSG-1", state="wrong")
    )

    assert response.status_code == 202, "still 2xx — retrying would never help"
    assert await _rows(tenant_id) == []
    assert enqueued == []


async def test_a_missing_client_state_stores_nothing(client, subscription, enqueued):
    tenant_id, _ = subscription
    payload = _notification("MSG-1")
    del payload["value"][0]["clientState"]

    response = await client.post("/api/graph/notifications", json=payload)

    assert response.status_code == 202
    assert await _rows(tenant_id) == []
    assert enqueued == []


@pytest.mark.parametrize("forged", ["é", "секрет", "🙂", "secret-x "])
async def test_a_non_ascii_client_state_is_rejected_not_crashed(
    client, subscription, enqueued, forged
):
    """`hmac.compare_digest` raises TypeError on non-ASCII `str`.

    Left unencoded, one accented character from an anonymous caller would 500
    the only unauthenticated endpoint in the system — breaking its own
    always-2xx contract and inviting a Graph retry storm.
    """
    tenant_id, _ = subscription

    response = await client.post(
        "/api/graph/notifications", json=_notification("MSG-1", state=forged)
    )

    assert response.status_code == 202
    assert await _rows(tenant_id) == []
    assert enqueued == []


async def test_an_empty_stored_secret_authorises_nothing(
    client, subscription, admin_session, enqueued
):
    """Nothing writes an empty client_state today. If anything ever did, an
    omitted clientState would compare equal and authorise anonymous inserts.
    """
    tenant_id, _ = subscription
    await admin_session.execute(
        text("UPDATE graph_subscriptions SET client_state = '' WHERE subscription_id = :s"),
        {"s": SUBSCRIPTION_ID},
    )
    await admin_session.commit()

    payload = _notification("MSG-1")
    payload["value"][0]["clientState"] = ""
    response = await client.post("/api/graph/notifications", json=payload)

    assert response.status_code == 202
    assert await _rows(tenant_id) == []


async def test_the_echoed_token_cannot_be_sniffed_into_another_type(client):
    """The body is caller-supplied; nosniff stops a browser reinterpreting it."""
    response = await client.post(
        "/api/graph/notifications", params={"validationToken": "<script>x</script>"}
    )

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("text/plain")


async def test_an_unknown_subscription_is_accepted_and_ignored(client, enqueued):
    """A subscription we deleted would otherwise be retried by Graph for hours."""
    response = await client.post(
        "/api/graph/notifications", json=_notification("MSG-9", sub="never-existed")
    )

    assert response.status_code == 202
    assert enqueued == []


async def test_a_replayed_notification_creates_one_row(client, subscription, enqueued):
    """Graph retries, delta sync overlaps, and the backfill covers the same
    window. Replay has to be free."""
    tenant_id, _ = subscription

    await client.post("/api/graph/notifications", json=_notification("MSG-DUP"))
    await client.post("/api/graph/notifications", json=_notification("MSG-DUP"))

    assert len(await _rows(tenant_id)) == 1
    assert len(enqueued) == 1, "the second notification must not queue a second job"


async def test_a_notification_without_a_message_id_is_skipped(client, subscription, enqueued):
    tenant_id, _ = subscription
    payload = _notification("ignored")
    payload["value"][0]["resourceData"] = {}

    response = await client.post("/api/graph/notifications", json=payload)

    assert response.status_code == 202
    assert await _rows(tenant_id) == []


async def test_the_row_survives_a_failed_enqueue(client, subscription, monkeypatch):
    """The durable half is already done. Answering 500 would make Graph retry a
    notification whose row exists, and the user would still see nothing —
    `rescan_stuck` is what closes this gap.
    """
    tenant_id, _ = subscription

    async def _fails(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(graph_webhook, "enqueue", _fails)

    response = await client.post("/api/graph/notifications", json=_notification("MSG-Q"))

    assert response.status_code == 202
    rows = await _rows(tenant_id)
    assert len(rows) == 1
    assert rows[0].processing_status == "pending", "left for the recovery sweep"


async def test_several_notifications_in_one_batch_are_all_recorded(
    client, subscription, enqueued
):
    tenant_id, _ = subscription
    payload = {"value": [_notification(f"MSG-{n}")["value"][0] for n in range(3)]}

    await client.post("/api/graph/notifications", json=payload)

    assert len(await _rows(tenant_id)) == 3
    assert len(enqueued) == 3


async def test_an_oversized_batch_is_capped(client, subscription, enqueued):
    """Unauthenticated and public: without a cap, one request could hold a
    worker for minutes doing a database round trip per element."""
    tenant_id, _ = subscription
    over = settings.GRAPH_MAX_NOTIFICATIONS_PER_REQUEST + 5
    payload = {"value": [_notification(f"MSG-{n}")["value"][0] for n in range(over)]}

    response = await client.post("/api/graph/notifications", json=payload)

    assert response.status_code == 202
    assert len(await _rows(tenant_id)) == settings.GRAPH_MAX_NOTIFICATIONS_PER_REQUEST


async def test_a_batch_resolves_each_subscription_once(client, subscription, enqueued):
    """One database lookup per distinct subscription, not per notification."""
    tenant_id, _ = subscription
    lookups: list[str] = []
    original = graph_webhook._resolve

    async def _counting(subscription_id: str):
        lookups.append(subscription_id)
        return await original(subscription_id)

    graph_webhook._resolve = _counting
    try:
        payload = {"value": [_notification(f"MSG-{n}")["value"][0] for n in range(5)]}
        await client.post("/api/graph/notifications", json=payload)
    finally:
        graph_webhook._resolve = original

    assert lookups == [SUBSCRIPTION_ID], "the resolver must be memoised per request"


# --- lifecycle --------------------------------------------------------------


@pytest.mark.parametrize(
    "event,job,argument",
    [
        ("reauthorizationRequired", "reauthorize_subscription", "subscription_id"),
        ("subscriptionRemoved", "recreate_subscription", "mailbox_id"),
        ("missed", "delta_sync_mailbox", "mailbox_id"),
    ],
)
async def test_lifecycle_events_queue_their_recovery(
    client, subscription, enqueued, event, job, argument
):
    """Without these, a revoked grant shows up only as notifications quietly
    stopping — which looks exactly like a slow week.

    The argument is asserted, not just the job name: `reauthorize_subscription`
    resolves the subscription itself, while the other two act on a mailbox, and
    handing either the wrong key would fail at the worker where nobody is
    watching.
    """
    tenant_id, mailbox_id = subscription

    response = await client.post(
        "/api/graph/lifecycle",
        json={
            "value": [
                {
                    "subscriptionId": SUBSCRIPTION_ID,
                    "clientState": CLIENT_STATE,
                    "lifecycleEvent": event,
                }
            ]
        },
    )

    assert response.status_code == 202
    assert len(enqueued) == 1
    name, kwargs = enqueued[0]
    assert name == job
    expected = SUBSCRIPTION_ID if argument == "subscription_id" else str(mailbox_id)
    assert kwargs[argument] == expected
    # Every job carries its tenant — a job that has to look one up needs a
    # function that bypasses RLS.
    assert kwargs["tenant_id"] == str(tenant_id)
    assert kwargs["mailbox_id"] == str(mailbox_id)


async def test_an_unknown_lifecycle_event_is_accepted_without_action(
    client, subscription, enqueued
):
    response = await client.post(
        "/api/graph/lifecycle",
        json={
            "value": [
                {
                    "subscriptionId": SUBSCRIPTION_ID,
                    "clientState": CLIENT_STATE,
                    "lifecycleEvent": "somethingNew",
                }
            ]
        },
    )

    assert response.status_code == 202
    assert enqueued == []


async def test_a_forged_lifecycle_event_does_nothing(client, subscription, enqueued):
    """Deleting a subscription is a state change; it needs the same proof."""
    response = await client.post(
        "/api/graph/lifecycle",
        json={
            "value": [
                {
                    "subscriptionId": SUBSCRIPTION_ID,
                    "clientState": "wrong",
                    "lifecycleEvent": "subscriptionRemoved",
                }
            ]
        },
    )

    assert response.status_code == 202
    assert enqueued == []


# --- shape -------------------------------------------------------------------


async def test_a_malformed_body_is_accepted_rather_than_retried(client):
    """Graph would retry a 4xx/5xx for hours; malformed input will not improve."""
    response = await client.post(
        "/api/graph/notifications",
        content=b"not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 202
