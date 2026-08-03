"""A paired WhatsApp device is private to the recruiter who paired it.

RLS scopes a destination to the agency, which is right for every channel that
is an agency account — a Telegram chat, the shared WABA number. It is wrong for
`whatsapp_linked`: that row is one person's handset, reached over their own
Baileys socket. Tenant scope alone let a colleague see it, tick their own job
orders onto it, or unlink it — and, because the settings screen hides the
pairing card once any `whatsapp_linked` destination is in the list, it also
stopped the colleague from ever adding their own device.

These tests pin both halves: the linked row is invisible and untouchable
outside its owner, and nothing about the tenant-wide Telegram feed changed.

allow-hardcode: this is a test module; literals below are fixture values.

ASGI transport, not TestClient, for the reason test_notifications_api.py gives.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP_LINKED,
    address_digest,
)
from app.models.wa_session import STATUS_CONNECTED
from app.services.notify.dispatch import emit
from app.services.notify.events import EVENT_OPPORTUNITY_NEW, OpportunityEvent

PHONE_A = "+6580000001"
PHONE_B = "+6580000002"


@pytest.fixture
async def client() -> httpx.AsyncClient:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
async def agency(admin_session):
    """One tenant, two recruiters, each with a connected WhatsApp device.

    Both devices connected on purpose: the bug only shows when B *could*
    legitimately link their own, and the panel that offers it is gated on B's
    own session being connected.
    """
    tenant_id = uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id.hex[:8]}"},
    )
    for uid, email in ((user_a, "a@agency.sg"), (user_b, "b@agency.sg")):
        await admin_session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :tid, :email, 'recruiter')"
            ),
            {"id": uid, "tid": tenant_id, "email": email},
        )
    for uid, phone in ((user_a, PHONE_A), (user_b, PHONE_B)):
        await admin_session.execute(
            text(
                "INSERT INTO wa_sessions (id, tenant_id, user_id, phone_e164, status) "
                "VALUES (:id, :tid, :uid, :phone, :status)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "uid": uid,
                "phone": phone,
                "status": STATUS_CONNECTED,
            },
        )
    await admin_session.commit()
    yield tenant_id, user_a, user_b
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _sign_in(client: httpx.AsyncClient, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Swap the session cookie. Both users share one client, because what is
    under test is exactly what changes between two callers of one endpoint."""
    from app.api.auth import SESSION_COOKIE, _session_serializer

    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )


async def _insert_destination(
    admin_session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    channel: str,
    address: str,
) -> uuid.UUID:
    destination_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, "
            " verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'ciphertext', :hash, now())"
        ),
        {
            "id": destination_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": channel,
            "hash": address_digest(address),
        },
    )
    await admin_session.commit()
    return destination_id


async def _channels(client: httpx.AsyncClient) -> list[dict]:
    response = await client.get("/api/notifications/settings")
    assert response.status_code == 200
    return response.json()["destinations"]


async def test_a_linked_device_is_absent_from_a_colleagues_settings(
    client, agency, admin_session
) -> None:
    """The read that made the frontend hide B's pairing card."""
    tenant_id, user_a, user_b = agency
    dest_a = await _insert_destination(
        admin_session, tenant_id, user_a, CHANNEL_WHATSAPP_LINKED, PHONE_A
    )

    _sign_in(client, tenant_id, user_a)
    assert [d["id"] for d in await _channels(client)] == [str(dest_a)]

    _sign_in(client, tenant_id, user_b)
    assert await _channels(client) == []


async def test_a_colleague_cannot_subscribe_unlink_or_rescope_a_linked_device(
    client, agency, admin_session
) -> None:
    """404, not 403: outside its owner the row does not exist, which is the
    same answer RLS gives across tenants."""
    tenant_id, user_a, user_b = agency
    dest_a = await _insert_destination(
        admin_session, tenant_id, user_a, CHANNEL_WHATSAPP_LINKED, PHONE_A
    )
    _sign_in(client, tenant_id, user_b)

    subscribe = await client.put(
        "/api/notifications/subscriptions",
        json={"destination_id": str(dest_a), "event_kinds": [EVENT_OPPORTUNITY_NEW]},
    )
    assert subscribe.status_code == 404

    rescope = await client.put(
        f"/api/notifications/destinations/{dest_a}/scope", json={"scope": "tenant"}
    )
    # Not the 400 the owner would get: B must not learn the row is there.
    assert rescope.status_code == 404

    unlink = await client.delete(f"/api/notifications/destinations/{dest_a}")
    assert unlink.status_code == 404

    # And nothing above touched it.
    async with tenant_session(tenant_id) as session:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification_destinations WHERE id = :id"
                ),
                {"id": dest_a},
            )
        ).scalar_one()
    assert remaining == 1


async def test_the_owner_still_controls_their_own_linked_device(
    client, agency, admin_session
) -> None:
    tenant_id, user_a, _user_b = agency
    dest_a = await _insert_destination(
        admin_session, tenant_id, user_a, CHANNEL_WHATSAPP_LINKED, PHONE_A
    )
    _sign_in(client, tenant_id, user_a)

    subscribe = await client.put(
        "/api/notifications/subscriptions",
        json={"destination_id": str(dest_a), "event_kinds": [EVENT_OPPORTUNITY_NEW]},
    )
    assert subscribe.status_code == 200

    # The owner sees the pre-existing refusal to share a handset with the
    # agency — a 400 that explains itself, not a 404.
    rescope = await client.put(
        f"/api/notifications/destinations/{dest_a}/scope", json={"scope": "tenant"}
    )
    assert rescope.status_code == 400

    unlink = await client.delete(f"/api/notifications/destinations/{dest_a}")
    assert unlink.status_code == 204


async def test_both_recruiters_can_hold_their_own_device_at_once(
    client, agency, admin_session
) -> None:
    """The consequence that was actually reported: B could never add theirs."""
    tenant_id, user_a, user_b = agency
    _sign_in(client, tenant_id, user_a)
    added_a = await client.post("/api/notifications/destinations/whatsapp-linked")
    assert added_a.status_code == 200

    _sign_in(client, tenant_id, user_b)
    added_b = await client.post("/api/notifications/destinations/whatsapp-linked")
    assert added_b.status_code == 200
    assert added_b.json()["destination_id"] != added_a.json()["destination_id"]

    # Two rows, and each recruiter sees exactly one — their own.
    for uid, expected in ((user_a, added_a), (user_b, added_b)):
        _sign_in(client, tenant_id, uid)
        assert [d["id"] for d in await _channels(client)] == [
            expected.json()["destination_id"]
        ]


async def test_a_tenant_wide_telegram_feed_is_still_shared(
    client, agency, admin_session
) -> None:
    """The deliberate non-change. A Telegram chat is an agency account, so
    both recruiters keep seeing and editing it."""
    tenant_id, user_a, user_b = agency
    shared = await _insert_destination(
        admin_session, tenant_id, None, CHANNEL_TELEGRAM, "shared-chat"
    )

    for uid in (user_a, user_b):
        _sign_in(client, tenant_id, uid)
        listed = await _channels(client)
        assert [d["id"] for d in listed] == [str(shared)]
        assert listed[0]["scope"] == "tenant"
        subscribe = await client.put(
            "/api/notifications/subscriptions",
            json={
                "destination_id": str(shared),
                "event_kinds": [EVENT_OPPORTUNITY_NEW],
            },
        )
        assert subscribe.status_code == 200


async def test_a_colleagues_own_telegram_destination_stays_visible(
    client, agency, admin_session
) -> None:
    """Also unchanged, and the reason the predicate names the channel rather
    than simply requiring ownership: a user-scoped Telegram destination has
    always been agency-visible, and existing tests assert it."""
    tenant_id, user_a, user_b = agency
    dest_a = await _insert_destination(
        admin_session, tenant_id, user_a, CHANNEL_TELEGRAM, "a-chat"
    )

    _sign_in(client, tenant_id, user_b)
    assert [d["id"] for d in await _channels(client)] == [str(dest_a)]


async def test_a_linked_destination_cannot_be_left_without_an_owner(
    agency, admin_session
) -> None:
    """The database's half of the rule. An ownerless linked row would be
    invisible to everyone and unsendable — `ck_destination_linked_has_owner`
    makes it unrepresentable rather than merely unreachable."""
    tenant_id, _user_a, _user_b = agency
    with pytest.raises(Exception, match="ck_destination_linked_has_owner"):
        await _insert_destination(
            admin_session, tenant_id, None, CHANNEL_WHATSAPP_LINKED, PHONE_A
        )
    await admin_session.rollback()


async def test_a_colleagues_event_never_reaches_a_personal_device(
    agency, admin_session
) -> None:
    """The dispatch half, pinned rather than fixed: `_SUBSCRIBERS` already
    matches `d.user_id` against the event's recipients, so a job order named
    for B produces no row for A's handset. Worth a test anyway — this is the
    half where a regression would put a colleague's candidate on someone's
    personal phone rather than merely on their settings screen.
    """
    tenant_id, user_a, user_b = agency
    dest_a = await _insert_destination(
        admin_session, tenant_id, user_a, CHANNEL_WHATSAPP_LINKED, PHONE_A
    )
    dest_b = await _insert_destination(
        admin_session, tenant_id, user_b, CHANNEL_WHATSAPP_LINKED, PHONE_B
    )
    for did in (dest_a, dest_b):
        await admin_session.execute(
            text(
                "INSERT INTO notification_subscriptions "
                "(id, tenant_id, destination_id, event_kind, active) "
                "VALUES (:id, :tid, :did, :kind, true)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": did,
                "kind": EVENT_OPPORTUNITY_NEW,
            },
        )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        ids = await emit(
            OpportunityEvent(
                kind=EVENT_OPPORTUNITY_NEW,
                tenant_id=tenant_id,
                opportunity_id=uuid.uuid4(),
                job_title="Engineer",
                company_name="Acme",
                location="Singapore",
                salary="SGD 8,000",
                recipient_user_ids=(user_b,),
            ),
            session,
        )

    async with tenant_session(tenant_id) as session:
        targeted = {
            row.destination_id
            for row in (
                await session.execute(
                    text(
                        "SELECT destination_id FROM notification_deliveries "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
            ).all()
        }
    assert targeted == {dest_b}
