"""WhatsApp outreach, step 1: draft, open, and the honesty constraint.

The platform never sends a message — it renders a draft and opens WhatsApp
Web; the recruiter presses send there. So `candidate_activities.status` may
only ever hold `'opened'`, and this file spends real effort proving the
database itself refuses `'sent'`, not just that the API declines to write it.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.main import app
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in


@pytest.fixture
async def agency_with_candidates():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {
        "with_phone": uuid.uuid4(),
        "no_phone": uuid.uuid4(),
        "single_token": uuid.uuid4(),
        "no_title": uuid.uuid4(),
    }
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, display_name, role) "
                "VALUES (:i, :t, :e, :d, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg", "d": "Wong"},
        )
        rows = [
            (ids["with_phone"], "Hui Ling Tan", "+6582217734", "Engineer"),
            (ids["no_phone"], "No Phone Tan", None, "Engineer"),
            (ids["single_token"], "Cher", "+6591234567", "Engineer"),
            (ids["no_title"], "Notitle Tan", "+6598765432", None),
        ]
        for cid, name, phone, title in rows:
            await s.execute(
                text(
                    "INSERT INTO candidates (id, tenant_id, full_name, phone_e164, "
                    "current_title) VALUES (:i, :t, :n, :p, :ct)"
                ),
                {"i": cid, "t": tid, "n": name, "p": phone, "ct": title},
            )
        await s.commit()
    yield tid, uid, ids
    async with AdminSessionLocal() as s:
        for table in ("candidate_activities", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_draft_renders_name_recruiter_and_agency(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (
            await http.get(f"/api/candidates/{ids['with_phone']}/whatsapp-draft")
        ).json()
    assert body["phone_e164"] == "+6582217734"
    assert body["message"].startswith("Hi Hui,")
    assert "Wong" in body["message"]
    assert f"agency-{tid.hex[:6]}" in body["message"]
    assert "Engineer opportunity" in body["message"]


async def test_a_single_token_name_is_used_whole(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (
            await http.get(f"/api/candidates/{ids['single_token']}/whatsapp-draft")
        ).json()
    assert body["message"].startswith("Hi Cher,")


async def test_no_title_reads_properly_with_no_blank_gap(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (
            await http.get(f"/api/candidates/{ids['no_title']}/whatsapp-draft")
        ).json()
    assert "regarding an opportunity" in body["message"]
    assert "  " not in body["message"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # The plain vowel rule, and the case that prompted this: the draft
        # hardcoded "a" and wrote "a Enrolled Nurse" — the exact title the
        # plan this feature came from uses as its example.
        ("Enrolled Nurse", "an Enrolled Nurse"),
        ("Engineer", "an Engineer"),
        ("Accountant", "an Accountant"),
        ("Operations Executive", "an Operations Executive"),
        ("Software Developer", "a Software Developer"),
        ("Marketing Manager", "a Marketing Manager"),
        # Initialisms go by the first letter's *name*: "aitch-are" takes
        # "an", "cue-ay" takes "a".
        ("HR Manager", "an HR Manager"),
        ("IT Support", "an IT Support"),
        ("QA Lead", "a QA Lead"),
        # u says "you", so it takes "a" despite being a vowel letter — and
        # "UX" is an initialism whose first letter says "you" as well.
        ("UX Designer", "a UX Designer"),
        ("Unit Manager", "a Unit Manager"),
        # Nothing to sound out; "a" is the safe default.
        ("3D Artist", "a 3D Artist"),
    ],
)
def test_the_article_follows_how_the_title_is_said(title, expected) -> None:
    """A unit test, not an API one: this is a sentence-level judgement and
    deserves to fail on its own terms rather than inside a 200 response."""
    from app.api.candidates import _whatsapp_draft_text

    message = _whatsapp_draft_text(
        candidate_first_name="Hui Ling",
        recruiter_name="Wong",
        agency_name="ABC Recruitment",
        job_title=title,
    )
    assert f"regarding {expected} opportunity." in message


async def test_no_phone_is_409_with_a_useful_message(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        resp = await http.get(f"/api/candidates/{ids['no_phone']}/whatsapp-draft")
    assert resp.status_code == 409
    assert "mobile number" in resp.json()["detail"]


async def test_logging_returns_201_and_reads_back_newest_first(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        first = await http.post(
            f"/api/candidates/{ids['with_phone']}/activities",
            json={"activity_type": "whatsapp_opened", "channel": "whatsapp", "message_text": "one"},
        )
        assert first.status_code == 201
        assert first.json()["status"] == "opened"
        assert first.json()["actor_name"] == "Wong"

        second = await http.post(
            f"/api/candidates/{ids['with_phone']}/activities",
            json={"activity_type": "whatsapp_opened", "channel": "whatsapp", "message_text": "two"},
        )
        assert second.status_code == 201

        listed = (await http.get(f"/api/candidates/{ids['with_phone']}/activities")).json()
    assert [row["message_text"] for row in listed["items"]] == ["two", "one"]


async def test_invalid_vocabulary_is_422(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            f"/api/candidates/{ids['with_phone']}/activities",
            json={"activity_type": "whatsapp_sent", "channel": "whatsapp"},
        )
    assert resp.status_code == 422


async def test_the_check_constraint_refuses_a_direct_insert_of_sent(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        with pytest.raises(IntegrityError) as exc_info:
            await s.execute(
                text(
                    "INSERT INTO candidate_activities "
                    "(id, tenant_id, candidate_id, user_id, activity_type, channel, status) "
                    "VALUES (:i, :t, :c, :u, 'whatsapp_opened', 'whatsapp', 'sent')"
                ),
                {"i": uuid.uuid4(), "t": tid, "c": ids["with_phone"], "u": uid},
            )
        await s.rollback()
    assert "ck_candidate_activities_status_known" in str(exc_info.value)


async def test_agency_b_cannot_read_or_write_agency_as_activities(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    tid_b, uid_b = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid_b, "n": f"agency-{tid_b.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid_b, "t": tid_b, "e": f"u{uid_b.hex[:6]}@other.sg"},
        )
        await s.commit()
    try:
        async with await _client_for(tid_b, uid_b) as http:
            draft = await http.get(f"/api/candidates/{ids['with_phone']}/whatsapp-draft")
            assert draft.status_code == 404

            create = await http.post(
                f"/api/candidates/{ids['with_phone']}/activities",
                json={"activity_type": "whatsapp_opened", "channel": "whatsapp"},
            )
            assert create.status_code == 404

            listing = await http.get(f"/api/candidates/{ids['with_phone']}/activities")
            assert listing.status_code == 404
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid_b})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid_b})
            await s.commit()


async def test_deleting_a_candidate_removes_their_activities(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    cid = ids["with_phone"]
    async with await _client_for(tid, uid) as http:
        await http.post(
            f"/api/candidates/{cid}/activities",
            json={"activity_type": "whatsapp_opened", "channel": "whatsapp"},
        )
        delete_resp = await http.delete(f"/api/candidates/{cid}")
        assert delete_resp.status_code == 204

    async with AdminSessionLocal() as s:
        remaining = (
            await s.execute(
                text("SELECT count(*) FROM candidate_activities WHERE candidate_id = :c"),
                {"c": cid},
            )
        ).scalar_one()
    assert remaining == 0
