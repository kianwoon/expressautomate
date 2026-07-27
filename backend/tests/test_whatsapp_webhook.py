"""Delivery outcomes and opt-outs arrive here. Without it, nothing disables.

ASGI transport, not TestClient: TestClient drives its own event loop and the
engine in app.db.session is pinned to the session-scoped one (see
tests/test_notifications_api.py, which hit this first).
"""

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.notification import address_digest

SECRET = "app-secret"


@pytest.fixture(autouse=True)
def _configure_secrets(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verify-me")


@pytest.fixture
async def client() -> httpx.AsyncClient:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


async def test_verification_handshake_echoes_the_challenge(client) -> None:
    response = await client.get(
        "/api/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


async def test_verification_with_a_wrong_token_is_refused(client) -> None:
    response = await client.get(
        "/api/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 403


async def test_an_unsigned_post_is_rejected(client) -> None:
    response = await client.post("/api/webhooks/whatsapp", json={"entry": []})
    assert response.status_code == 401


async def test_an_empty_configured_secret_rejects_rather_than_passes(
    client, monkeypatch
) -> None:
    """A blank WHATSAPP_APP_SECRET must not be treated as 'no check needed'."""
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    raw, headers = _signed({"entry": []})
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 401


async def test_a_wrongly_signed_post_is_rejected(client) -> None:
    raw, headers = _signed({"entry": []})
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 401


async def test_a_stop_message_suppresses_the_number(client, admin_session) -> None:
    """Meta's opt-out is per phone number, and we share one across tenants."""
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "6591234567", "text": {"body": "STOP"}}
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw, headers = _signed(body)
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM whatsapp_suppressions WHERE address_hash = :h"),
            {"h": address_digest("+6591234567")},
        )
    ).scalar_one()
    assert count == 1

    await admin_session.execute(text("DELETE FROM whatsapp_suppressions"))
    await admin_session.commit()


async def test_a_substring_match_is_not_an_opt_out(client, admin_session) -> None:
    """'stop by the office tomorrow' is not an opt-out — match the whole
    trimmed, lowercased message, never a substring."""
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "6591234568",
                                    "text": {"body": "stop by the office tomorrow"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw, headers = _signed(body)
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM whatsapp_suppressions WHERE address_hash = :h"),
            {"h": address_digest("+6591234568")},
        )
    ).scalar_one()
    assert count == 0


async def test_a_failed_status_records_the_delivery_error(
    client, admin_session
) -> None:
    tenant_id, dest_id, delivery_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, 'whatsapp', 'x', :hash, now())"
        ),
        {"id": dest_id, "tid": tenant_id, "hash": address_digest("+6591234567")},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_deliveries "
            "(id, tenant_id, destination_id, event_kind, subject_id, status, "
            " provider_message_id) "
            "VALUES (:id, :tid, :did, 'opportunity.new', :sub, 'sent', 'wamid.ABC')"
        ),
        {
            "id": delivery_id,
            "tid": tenant_id,
            "did": dest_id,
            "sub": uuid.uuid4(),
        },
    )
    await admin_session.commit()

    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.ABC",
                                    "status": "failed",
                                    "errors": [{"code": 131026, "title": "undeliverable"}],
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw, headers = _signed(body)
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200

    status = (
        await admin_session.execute(
            text("SELECT status FROM notification_deliveries WHERE id = :id"),
            {"id": delivery_id},
        )
    ).scalar_one()
    assert status == "failed"

    disabled_at = (
        await admin_session.execute(
            text("SELECT disabled_at FROM notification_destinations WHERE id = :id"),
            {"id": dest_id},
        )
    ).scalar_one()
    assert disabled_at is not None

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


async def test_a_malformed_payload_does_not_500(client) -> None:
    """Meta retries a 5xx and eventually disables the webhook entirely."""
    raw, headers = _signed({"entry": [{"changes": [{}]}]})
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200


async def test_statuses_not_a_list_does_not_500(client) -> None:
    raw, headers = _signed(
        {"entry": [{"changes": [{"value": {"statuses": "not-a-list"}}]}]}
    )
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200


async def test_a_status_with_no_errors_does_not_500(client, admin_session) -> None:
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [{"id": "wamid.NOPE", "status": "failed"}]
                        }
                    }
                ]
            }
        ]
    }
    raw, headers = _signed(body)
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200


async def test_a_message_with_no_text_does_not_500(client) -> None:
    body = {
        "entry": [{"changes": [{"value": {"messages": [{"from": "6591234567"}]}}]}]
    }
    raw, headers = _signed(body)
    response = await client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200
