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
    # The whole name, not a token of it. "Hui Ling Tan" written the other way
    # round is "Tan Hui Ling", and no rule here can tell which order a given
    # row used — so greeting anything shorter risks "Hi Tan" (a stranger
    # addressed by surname) or "Hi Hui" (half a given name). The recruiter
    # shortens it; they know the person and this code does not.
    assert body["message"].startswith("Hi Hui Ling Tan,")
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
        ("MRT Station Manager", "an MRT Station Manager"),
        # Caps lock is not an initialism. A title typed or extracted in full
        # caps must still be read as the words it is, or "SENIOR ENGINEER"
        # comes out as "an SENIOR ENGINEER".
        ("SENIOR ENGINEER", "a SENIOR ENGINEER"),
        ("MARKETING MANAGER", "a MARKETING MANAGER"),
        ("ENROLLED NURSE", "an ENROLLED NURSE"),
        # Four letters or fewer and capitalised is not enough on its own:
        # these are shouted words, not initials.
        ("HEAD OF SALES", "a HEAD OF SALES"),
        ("LEAD ENGINEER", "a LEAD ENGINEER"),
        # Four-letter initialisms are why the threshold is 4 and not 3 —
        # NTUC and SMRT are everyday employers here.
        ("NTUC Officer", "an NTUC Officer"),
        ("SMRT Technician", "an SMRT Technician"),
        # One word in caps with nothing to shout alongside it stays an
        # initialism; there is no sentence for caps lock to have shouted.
        ("HR", "an HR"),
        # Nothing to sound out; "a" is the safe default.
        ("3D Artist", "a 3D Artist"),
    ],
)
def test_the_article_follows_how_the_title_is_said(title, expected) -> None:
    """A unit test, not an API one: this is a sentence-level judgement and
    deserves to fail on its own terms rather than inside a 200 response."""
    from app.api.candidate_whatsapp import whatsapp_draft_text

    message = whatsapp_draft_text(
        candidate_greeting_name="Hui Ling",
        recruiter_name="Wong",
        agency_name="ABC Recruitment",
        job_title=title,
    )
    assert f"regarding {expected} opportunity." in message


async def test_draft_uses_preferred_name_over_display_name(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE users SET preferred_name = :p WHERE id = :i"),
            {"p": "W.", "i": uid},
        )
        await s.commit()
    try:
        async with await _client_for(tid, uid) as http:
            body = (
                await http.get(f"/api/candidates/{ids['with_phone']}/whatsapp-draft")
            ).json()
        assert "This is W. from" in body["message"]
        assert "Wong" not in body["message"], "preferred_name must take priority"
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE users SET preferred_name = NULL WHERE id = :i"), {"i": uid}
            )
            await s.commit()


async def test_draft_falls_back_to_display_name_when_no_preferred_name(
    agency_with_candidates,
) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (
            await http.get(f"/api/candidates/{ids['with_phone']}/whatsapp-draft")
        ).json()
    assert "This is Wong from" in body["message"]


async def test_draft_with_no_name_at_all_has_no_email_and_reads_properly(
    agency_with_candidates,
) -> None:
    """Neither `preferred_name` nor `display_name` set: the message must not
    fall back to the recruiter's email, and the sentence must still read as a
    complete sentence rather than a gap where the name would go."""
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        await s.execute(text("UPDATE users SET display_name = NULL WHERE id = :i"), {"i": uid})
        await s.commit()
    try:
        async with await _client_for(tid, uid) as http:
            body = (
                await http.get(f"/api/candidates/{ids['with_phone']}/whatsapp-draft")
            ).json()
        message = body["message"]
        assert "@" not in message
        assert f"I am writing from agency-{tid.hex[:6]}." in message
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE users SET display_name = :d WHERE id = :i"), {"d": "Wong", "i": uid}
            )
            await s.commit()


async def test_actor_name_on_timeline_still_falls_back_to_email(agency_with_candidates) -> None:
    """`actor_name` is seen only by colleagues in this tenant, so the email
    fallback stays — unlike the candidate-facing draft."""
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        await s.execute(text("UPDATE users SET display_name = NULL WHERE id = :i"), {"i": uid})
        email = (
            await s.execute(text("SELECT email FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
        await s.commit()
    try:
        async with await _client_for(tid, uid) as http:
            response = await http.post(
                f"/api/candidates/{ids['with_phone']}/activities",
                json={"activity_type": "whatsapp_opened", "channel": "whatsapp"},
            )
        assert response.json()["actor_name"] == email
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(
                text("UPDATE users SET display_name = :d WHERE id = :i"), {"d": "Wong", "i": uid}
            )
            await s.commit()


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
    # `whatsapp_sent` is a legal value in the table now, but this endpoint is
    # the *popup* path and writes `opened`; a `whatsapp_sent` row may only be
    # written by the code that actually saw a send (see
    # `test_candidate_whatsapp_send.py`), so the manual-log route still
    # refuses the word.
    assert resp.status_code == 422


@pytest.mark.parametrize("status", ["opened", "sent", "failed"])
async def test_the_check_constraint_accepts_the_whole_vocabulary(
    agency_with_candidates, status
) -> None:
    """Three statuses, and exactly three.

    `opened` is the popup path; `sent` and `failed` arrived with the WA
    gateway, which holds the socket and so does observe the handover. `sent`
    means WhatsApp returned a message id — not delivered, never read.
    """
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_activities "
                "(id, tenant_id, candidate_id, user_id, activity_type, channel, status) "
                "VALUES (:i, :t, :c, :u, 'whatsapp_sent', 'whatsapp', :s)"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": ids["with_phone"], "u": uid, "s": status},
        )
        await s.commit()


@pytest.mark.parametrize("status", ["delivered", "read", "queued", "SENT", ""])
async def test_the_check_constraint_still_refuses_anything_else(
    agency_with_candidates, status
) -> None:
    """`delivered` and `read` are the two that matter here: no receipts are
    ingested in v1, so the database refuses to record a claim nothing in this
    system has observed (§15) — not merely undocumented, impossible."""
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        with pytest.raises(IntegrityError) as exc_info:
            await s.execute(
                text(
                    "INSERT INTO candidate_activities "
                    "(id, tenant_id, candidate_id, user_id, activity_type, channel, status) "
                    "VALUES (:i, :t, :c, :u, 'whatsapp_sent', 'whatsapp', :s)"
                ),
                {"i": uuid.uuid4(), "t": tid, "c": ids["with_phone"], "u": uid, "s": status},
            )
        await s.rollback()
    assert "ck_candidate_activities_status_known" in str(exc_info.value)


async def test_the_check_constraint_refuses_an_unknown_activity_type(
    agency_with_candidates,
) -> None:
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        with pytest.raises(IntegrityError) as exc_info:
            await s.execute(
                text(
                    "INSERT INTO candidate_activities "
                    "(id, tenant_id, candidate_id, user_id, activity_type, channel, status) "
                    "VALUES (:i, :t, :c, :u, 'email_sent', 'whatsapp', 'sent')"
                ),
                {"i": uuid.uuid4(), "t": tid, "c": ids["with_phone"], "u": uid},
            )
        await s.rollback()
    assert "ck_candidate_activities_type_known" in str(exc_info.value)


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
