"""What the matcher may decide on its own, and what it must leave to a person.

The matcher may: link a message to a client whose domain it already knows,
and create a new unconfirmed proposal. It may not: confirm anything, merge
anything, or un-archive anything. Every test here is about that boundary —
the storage is the easy part.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.client_matching import match_client
from tests.conftest import AdminSessionLocal, cleanup_tenant


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    # `_seeded_message` (below) inserts mailboxes/email_messages behind some
    # of these tests; cleanup_tenant clears those too, not just clients.
    await cleanup_tenant(tid)


async def _status_of(tenant_id: uuid.UUID, client_id: uuid.UUID) -> str:
    async with tenant_session(tenant_id) as s:
        return (
            await s.execute(text("SELECT status FROM clients WHERE id = :i"), {"i": client_id})
        ).scalar_one()


async def _mention_count(tenant_id: uuid.UUID, client_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM client_mentions WHERE client_id = :i"), {"i": client_id}
            )
        ).scalar_one()


async def test_an_unknown_domain_becomes_an_unconfirmed_proposal(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    assert cid is not None
    assert await _status_of(agency, cid) == "unconfirmed"


async def test_the_same_domain_twice_is_one_client(agency) -> None:
    async with tenant_session(agency) as s:
        first = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        second = await match_client(s, agency, None, "jobs@acme.com.sg", "ACME")
        await s.commit()
    assert first == second


async def test_a_free_provider_never_keys_a_client(agency) -> None:
    async with tenant_session(agency) as s:
        a = await match_client(s, agency, None, "alice@gmail.com", "Acme Pte Ltd")
        b = await match_client(s, agency, None, "bob@gmail.com", "Globex Ltd")
        await s.commit()
    assert a != b
    async with tenant_session(agency) as s:
        domains = (await s.execute(text("SELECT email_domain FROM clients"))).scalars().all()
    assert domains == [None, None]


async def test_a_name_match_attaches_but_does_not_confirm(agency) -> None:
    async with tenant_session(agency) as s:
        first = await match_client(s, agency, None, "alice@gmail.com", "Acme Pte Ltd")
        second = await match_client(s, agency, None, "bob@gmail.com", "ACME PTE. LTD.")
        await s.commit()
    assert first == second
    assert await _status_of(agency, first) == "unconfirmed"


async def test_a_null_sender_falls_through_to_the_name(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, None, "Acme Pte Ltd")
        await s.commit()
    assert cid is not None
    async with tenant_session(agency) as s:
        domain = (
            await s.execute(text("SELECT email_domain FROM clients WHERE id = :i"), {"i": cid})
        ).scalar_one()
    assert domain is None


async def test_nothing_to_match_on_produces_nothing(agency) -> None:
    async with tenant_session(agency) as s:
        assert await match_client(s, agency, None, None, None) is None
        assert await match_client(s, agency, None, None, "   ") is None
        await s.commit()


async def _seeded_message(tenant_id: uuid.UUID) -> uuid.UUID:
    """Schema requires a real mailbox behind every message; the brief's
    bare insert predates `mailbox_id` becoming NOT NULL, so seed one here."""
    mailbox_id = uuid.uuid4()
    message_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes "
                "(id, tenant_id, ms_user_id, scope, folder_id, retention_months) "
                "VALUES (:i, :t, 'msuser', 'whole_inbox', 'inbox', 12) "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": mailbox_id, "t": tenant_id},
        )
        await s.execute(
            text(
                "INSERT INTO email_messages "
                "(id, tenant_id, mailbox_id, graph_message_id, subject) "
                "VALUES (:i, :t, :m, :g, 'x') ON CONFLICT DO NOTHING"
            ),
            {"i": message_id, "t": tenant_id, "m": mailbox_id, "g": str(message_id)},
        )
        await s.commit()
    return message_id


async def test_reprocessing_the_same_message_adds_no_second_mention(agency) -> None:
    message_id = await _seeded_message(agency)

    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, message_id, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    async with tenant_session(agency) as s:
        again = await match_client(s, agency, message_id, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()

    assert cid == again
    assert await _mention_count(agency, cid) == 1


async def test_re_seeing_an_archived_client_does_not_resurrect_it(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    async with tenant_session(agency) as s:
        await s.execute(
            text("UPDATE clients SET status = 'archived' WHERE id = :i"), {"i": cid}
        )
        await s.commit()

    async with tenant_session(agency) as s:
        again = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()

    # Same row — the archived client still holds the domain index slot, so an
    # insert here would be a unique violation, not a new client.
    assert again == cid
    assert await _status_of(agency, cid) == "archived"


async def test_reprocessing_with_no_message_id_adds_no_second_mention(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    async with tenant_session(agency) as s:
        again = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()

    assert cid == again
    assert await _mention_count(agency, cid) == 1


async def test_a_two_hop_merge_chain_lands_on_the_final_survivor(agency) -> None:
    async with tenant_session(agency) as s:
        a = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        b = await match_client(s, agency, None, "hr@acme-group.com", "Acme Group")
        c = await match_client(s, agency, None, "hr@acme-holdings.com", "Acme Holdings")
        await s.commit()
    async with tenant_session(agency) as s:
        await s.execute(
            text(
                "UPDATE clients SET status = 'merged', merged_into_client_id = :w WHERE id = :l"
            ),
            {"w": b, "l": a},
        )
        await s.execute(
            text(
                "UPDATE clients SET status = 'merged', merged_into_client_id = :w WHERE id = :l"
            ),
            {"w": c, "l": b},
        )
        await s.commit()

    async with tenant_session(agency) as s:
        landed = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    assert landed == c


async def test_re_seeing_a_merged_client_lands_on_the_survivor(agency) -> None:
    async with tenant_session(agency) as s:
        loser = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        winner = await match_client(s, agency, None, "hr@acme-group.com", "Acme Group")
        await s.commit()
    async with tenant_session(agency) as s:
        await s.execute(
            text(
                "UPDATE clients SET status = 'merged', merged_into_client_id = :w WHERE id = :l"
            ),
            {"w": winner, "l": loser},
        )
        await s.commit()

    async with tenant_session(agency) as s:
        landed = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    assert landed == winner


async def test_the_matcher_carries_its_own_tenant_predicate(agency) -> None:
    """Defence in depth, exactly as `candidate_matching` already had it.

    RLS is what enforces isolation in production, and it holds. This test runs
    the matcher on the RLS-bypassing admin session — the one place where the
    policy is not doing the work — so it can see whether the *queries* also
    fail closed. Before the fix the domain and name lookups had no tenant
    predicate at all, and this found the other agency's client; the sibling
    candidate matcher was already written the other way, and two near-copy
    modules disagreeing about what guarantees isolation is how a future reader
    gets it wrong.
    """
    other = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": other, "n": f"agency-{other.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                "email_domain, status) VALUES (:i, :t, 'Acme', 'acme pte ltd', "
                "'acme.com.sg', 'unconfirmed')"
            ),
            {"i": uuid.uuid4(), "t": other},
        )
        await s.commit()

    try:
        async with AdminSessionLocal() as s:
            matched = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
            await s.commit()
            owner = (
                await s.execute(
                    text("SELECT tenant_id FROM clients WHERE id = :i"), {"i": matched}
                )
            ).scalar_one()
        assert owner == agency
    finally:
        await cleanup_tenant(other)
