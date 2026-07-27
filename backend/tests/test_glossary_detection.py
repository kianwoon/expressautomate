"""What the shorthand scanner finds, and — mostly — what it refuses to find.

The refusals carry the weight here. A missed code costs a recruiter one glance
at the email; a false one puts a demographic requirement in a client's mouth
that the client never wrote, which is the failure this whole design exists to
avoid. So most of these tests assert an empty result.

No model is called anywhere in this file, and there is nothing to stub: that is
the point of the feature. Decoding is a scan plus a dictionary lookup.
"""

import uuid

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.core.config import settings
from app.db.rls import tenant_session
from app.db.session import engine
from app.main import app
from app.models import User
from app.models.glossary import normalise
from app.services.ingest.glossary import GlossaryEntry, detect


@pytest.fixture(autouse=True)
def _scanner_settings(monkeypatch):
    """CI has no `.env`, so every setting the scanner reads is pinned here."""
    monkeypatch.setattr(settings, "GLOSSARY_MIN_CODE_LENGTH", 2)
    monkeypatch.setattr(settings, "GLOSSARY_BOUNDARY_CHARS", "(),;:!?\"'[]{}<>*")
    monkeypatch.setattr(settings, "GLOSSARY_WEAK_BOUNDARY_CHARS", ".")
    monkeypatch.setattr(settings, "GLOSSARY_MAX_CODES", 500)


CF = GlossaryEntry(code="C/F", meaning="Chinese female", attribute="race")
OO = GlossaryEntry(code="o/o", meaning="open to all", attribute=None)
CO = GlossaryEntry(code="C/O", meaning="Chinese, gender open", attribute="race")
COF = GlossaryEntry(code="C/O/F", meaning="Chinese, open, female", attribute="race")


def test_a_code_is_found_with_its_agency_meaning():
    source = "Client wants C/F for the front desk."
    (found,) = detect(source, [CF])
    assert found.code == "C/F"
    assert found.meaning == "Chinese female"
    assert found.attribute == "race"


def test_offsets_reslice_to_the_matched_code():
    """The contract every consumer of these rows depends on.

    `extraction_evidence` earns trust by pointing at real characters; a code
    row that pointed at the wrong ones would be worse than no row, because a
    reviewer would follow it, see unrelated words, and stop trusting the ones
    that were right.
    """
    source = "Prefer C/F.\nBackup: o/o candidates welcome."
    for found in detect(source, [CF, OO]):
        assert source[found.start_char : found.end_char] == found.code


def test_a_code_is_not_found_inside_a_larger_token():
    """`ABC/FGH` is not `C/F`. The naive substring test is the whole hazard."""
    assert detect("Part number ABC/FGH shipped", [CF]) == []


def test_a_code_is_not_found_when_a_separator_extends_it():
    """`C/F` inside `C/F/M` is half of what was written."""
    assert detect("Requirement C/F/M please", [CF]) == []


def test_a_full_stop_ends_a_sentence_but_not_a_code():
    assert len(detect("They asked for C/F.", [CF])) == 1
    assert detect("Code C.F.M applies", [CF]) == []


def test_spacing_around_the_separator_is_tolerated():
    (found,) = detect("Looking for C / F only", [CF])
    assert found.code == "C / F"
    assert found.meaning == "Chinese female"


def test_matching_is_case_insensitive():
    (found,) = detect("looking for c/f", [CF])
    assert found.code == "c/f"


def test_dropping_the_separator_does_not_match():
    """`CF` is not `C/F` — otherwise any two-letter word could stand in."""
    assert detect("the CF report", [CF]) == []


def test_every_occurrence_is_returned_in_order():
    source = "Role 1: C/F. Role 2: C/F. Role 3: o/o."
    found = detect(source, [CF, OO])
    assert [f.meaning for f in found] == [
        "Chinese female",
        "Chinese female",
        "open to all",
    ]
    assert [f.start_char for f in found] == sorted(f.start_char for f in found)
    for f in found:
        assert source[f.start_char : f.end_char] == f.code


def test_a_code_absent_from_the_glossary_is_not_decoded():
    """There is no built-in code list. An agency that has not defined `o/o`
    gets no interpretation of it, which is correct — we do not know theirs."""
    assert detect("Client says o/o", [CF]) == []


def test_an_empty_glossary_decodes_nothing():
    assert detect("C/F o/o C/O", []) == []


def test_the_longer_code_wins_an_overlap():
    """Otherwise the more specific definition the agency wrote never fires."""
    (found,) = detect("Requirement C/O/F confirmed", [CO, COF])
    assert found.meaning == "Chinese, open, female"
    assert found.code == "C/O/F"


def test_a_code_shorter_than_the_minimum_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "GLOSSARY_MIN_CODE_LENGTH", 4)
    assert detect("Client wants C/F", [CF]) == []


def test_punctuation_does_not_pad_a_code_past_the_minimum():
    """`C/` is two characters and one letter, and the letter does the matching.

    Counting the slash would smuggle a one-character code past the check that
    exists to stop exactly that.
    """
    padded = GlossaryEntry(code="C/", meaning="Chinese", attribute="race")
    assert detect("Client wants C/ candidates", [padded]) == []


def test_duplicate_spellings_decode_the_text_once():
    """`C/F` and `c / f` are one code under `normalise`, so one match."""
    spelt_differently = GlossaryEntry(code="c / f", meaning="Chinese female")
    assert normalise(CF.code) == normalise(spelt_differently.code)
    assert len(detect("Wants C/F today", [CF, spelt_differently])) == 1


def test_the_shared_normalise_defines_sameness():
    """Guards the seam: if the API's notion of "the same code" drifts from the
    scanner's, a code the agency can see in their glossary silently stops
    matching the email that contains it, with nothing on screen to explain it.
    """
    assert normalise("C/F") == normalise("c / f") == normalise("C.F.")


# --- Persistence -----------------------------------------------------------


@pytest.fixture
async def tenants(admin_session):
    """Two agencies, so isolation can be asserted rather than assumed."""
    ids = [uuid.uuid4(), uuid.uuid4()]
    for tid in ids:
        await admin_session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tid, "name": f"agency-{tid}", "slug": f"agency-{tid}"},
        )
    await admin_session.commit()
    yield ids
    for tid in ids:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": tid}
        )
    await admin_session.commit()


async def _seed_opportunity(admin_session, tenant_id: uuid.UUID) -> uuid.UUID:
    """An opportunity row with only its NOT NULL columns, via the admin role.

    The fixture bypasses RLS on purpose: it is setting up the world, not
    exercising the policy, and the assertions that follow all run through
    `tenant_session`.
    """
    mailbox_id, email_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes "
            "(id, tenant_id, ms_user_id, scope, folder_id, status, retention_months) "
            "VALUES (:id, :t, :ms, 'mailbox', 'inbox', 'active', 12)"
        ),
        {"id": mailbox_id, "t": tenant_id, "ms": str(uuid.uuid4())},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id) "
            "VALUES (:id, :t, :m, :g)"
        ),
        {"id": email_id, "t": tenant_id, "m": mailbox_id, "g": str(uuid.uuid4())},
    )
    await admin_session.execute(
        text(
            "INSERT INTO opportunities "
            "(id, tenant_id, email_message_id, review_status, quality_state) "
            "VALUES (:id, :t, :e, 'ready', 'verified')"
        ),
        {"id": opportunity_id, "t": tenant_id, "e": email_id},
    )
    await admin_session.commit()
    return opportunity_id


async def _write_code(
    tenant_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    meaning: str,
    *,
    stamped_tenant: uuid.UUID | None = None,
):
    """Write a decoded code, optionally stamping a tenant the session is not.

    `stamped_tenant` exists so the isolation test can attempt the thing RLS is
    supposed to forbid. Without it the helper set `tenant_id` to the session's
    own tenant, so `WITH CHECK` had nothing to object to and the test that
    claimed to prove a cross-tenant write fails was proving nothing at all.
    """
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO opportunity_codes "
                "(id, tenant_id, opportunity_id, code, meaning, attribute, "
                " start_char, end_char) "
                "VALUES (:id, :t, :o, 'C/F', :m, 'race', 13, 16)"
            ),
            {
                "id": uuid.uuid4(),
                "t": stamped_tenant or tenant_id,
                "o": opportunity_id,
                "m": meaning,
            },
        )


async def test_meaning_is_snapshotted_not_joined(tenants, admin_session):
    """Editing the glossary must not rewrite what January's job orders said.

    An FK to `glossary_codes` would always show the current definition, which
    would make the audit trail change retroactively — evidence of nothing.
    """
    tenant_id = tenants[0]
    opportunity_id = await _seed_opportunity(admin_session, tenant_id)
    await _write_code(tenant_id, opportunity_id, "Chinese female")

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO glossary_codes "
                "(id, tenant_id, code, code_normalised, meaning, source) "
                "VALUES (:id, :t, 'C/F', 'cf', :m, 'agency')"
            ),
            {"id": uuid.uuid4(), "t": tenant_id, "m": "Chinese female"},
        )
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE glossary_codes SET meaning = :m WHERE code_normalised = 'cf'"),
            {"m": "Cantonese speaker, female"},
        )
    async with tenant_session(tenant_id) as session:
        stored = (
            await session.execute(
                text(
                    "SELECT meaning FROM opportunity_codes "
                    "WHERE opportunity_id = :o"
                ),
                {"o": opportunity_id},
            )
        ).scalar_one()
    assert stored == "Chinese female"


async def test_another_tenant_cannot_read_a_decoded_code(tenants, admin_session):
    owner, other = tenants
    opportunity_id = await _seed_opportunity(admin_session, owner)
    await _write_code(owner, opportunity_id, "Chinese female")

    async with tenant_session(other) as session:
        rows = (
            await session.execute(text("SELECT id FROM opportunity_codes"))
        ).scalars().all()
    assert rows == []


async def test_a_code_cannot_be_written_under_another_tenants_id(tenants, admin_session):
    """WITH CHECK, not just USING: a write must fail, not vanish.

    The row must carry a tenant the session is not scoped to — that is the
    whole claim. An earlier version of this stamped the session's own tenant
    and asserted an error anyway, so it tested nothing and failed in CI for the
    right reason by accident: there was no violation to detect.
    """
    owner, other = tenants
    opportunity_id = await _seed_opportunity(admin_session, owner)
    with pytest.raises(DBAPIError):
        await _write_code(
            other, opportunity_id, "Chinese female", stamped_tenant=owner
        )


# --- The list endpoint ------------------------------------------------------


async def _sign_in(client, admin_session, tenant_id: uuid.UUID) -> None:
    user_id = uuid.uuid4()
    admin_session.add(
        User(id=user_id, tenant_id=tenant_id, email=f"{user_id.hex[:8]}@example.test")
    )
    await admin_session.commit()
    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


async def test_the_list_returns_the_decoded_codes(tenants, admin_session, client):
    """Storing the decode is half the feature; the recruiter has to see it."""
    tenant_id = tenants[0]
    opportunity_id = await _seed_opportunity(admin_session, tenant_id)
    await _write_code(tenant_id, opportunity_id, "Chinese female")
    await _sign_in(client, admin_session, tenant_id)

    response = await client.get("/api/opportunities")
    assert response.status_code == 200
    (item,) = [i for i in response.json()["items"] if i["id"] == str(opportunity_id)]
    assert item["codes"] == [
        {
            "code": "C/F",
            "meaning": "Chinese female",
            "attribute": "race",
            "start_char": 13,
            "end_char": 16,
        }
    ]
    # Derived server-side: a rule the client re-implements is a rule that can
    # differ per client, on the rows where being wrong matters most.
    assert item["references_protected_attribute"] is True


async def test_a_vacancy_with_no_codes_says_so_rather_than_omitting_them(
    tenants, admin_session, client
):
    tenant_id = tenants[0]
    opportunity_id = await _seed_opportunity(admin_session, tenant_id)
    await _sign_in(client, admin_session, tenant_id)

    response = await client.get("/api/opportunities")
    (item,) = [i for i in response.json()["items"] if i["id"] == str(opportunity_id)]
    assert item["codes"] == []
    assert item["references_protected_attribute"] is False


async def test_the_page_costs_one_query_for_every_vacancys_codes(
    tenants, admin_session, client
):
    """The N+1 guard. Codes are fetched for the whole page in one statement, so
    a page of fifty vacancies does not become fifty round trips."""
    tenant_id = tenants[0]
    for _ in range(3):
        opportunity_id = await _seed_opportunity(admin_session, tenant_id)
        await _write_code(tenant_id, opportunity_id, "Chinese female")
    await _sign_in(client, admin_session, tenant_id)

    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, *args):  # noqa: ANN001, ANN202
        statements.append(statement)

    try:
        response = await client.get("/api/opportunities")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert sum("opportunity_codes" in s for s in statements) == 1


async def test_the_table_is_force_row_level_security(admin_session):
    """ENABLE alone leaves the owner — who runs migrations — outside the policy,
    so the table would look protected in the catalogue and not be."""
    enabled, forced = (
        await admin_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'opportunity_codes'"
            )
        )
    ).one()
    assert enabled and forced
