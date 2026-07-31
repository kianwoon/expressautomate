"""The transitional tenant-wide share that keeps ownership from landing as a blackout.

The SQL is imported from the revision file itself, so these pin the statements
that actually run rather than a copy of them.

allow-hardcode: SQL statements and fixture names, not configuration.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.models.candidate import Candidate
from app.models.candidate_share import CandidateShare
from app.services.visibility import visible_candidates
from tests.conftest import make_candidate, make_user

_REVISION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260731_1100_transitional_tenant_wide_candidate_shares.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location("_transitional_shares", _REVISION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_colleagues_candidate_stays_visible(admin_session, seeded) -> None:
    """The whole point: after the migration, nothing disappears from view."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-transitional-visible")
    colleague = await make_user(admin_session, tenant_id, "colleague@transitional.test")
    theirs = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    archived = await make_candidate(
        admin_session, tenant_id, owner_id=colleague, record_status="archived"
    )
    await admin_session.commit()

    before = set(
        (
            await admin_session.execute(
                select(Candidate.id).where(visible_candidates(me, "recruiter"))
            )
        )
        .scalars()
        .all()
    )
    assert theirs not in before, "a colleague's candidate was already visible"

    await admin_session.execute(text(_revision_module().TRANSITIONAL_SHARE_SQL))
    await admin_session.commit()

    after = set(
        (
            await admin_session.execute(
                select(Candidate.id).where(visible_candidates(me, "recruiter"))
            )
        )
        .scalars()
        .all()
    )
    assert theirs in after
    assert archived in after, "record_status was filtered — every row must get a share"

    row = (
        await admin_session.execute(
            select(CandidateShare).where(
                CandidateShare.candidate_id == theirs,
                CandidateShare.scope == CandidateShare.SCOPE_TENANT,
            )
        )
    ).scalar_one()
    assert row.shared_by_user_id is None, "a user id here would misattribute the decision"
    assert row.shared_with_user_id is None


@pytest.mark.asyncio
async def test_rerunning_the_upgrade_does_not_raise(admin_session, seeded) -> None:
    """Idempotent against the partial unique index, so a re-run is safe."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-transitional-idempotent")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=me)
    await admin_session.commit()

    sql = _revision_module().TRANSITIONAL_SHARE_SQL
    await admin_session.execute(text(sql))
    await admin_session.commit()
    await admin_session.execute(text(sql))
    await admin_session.commit()

    count = (
        await admin_session.execute(
            select(CandidateShare.id).where(
                CandidateShare.candidate_id == candidate_id,
                CandidateShare.scope == CandidateShare.SCOPE_TENANT,
            )
        )
    ).all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_downgrade_spares_a_deliberate_broadcast(admin_session, seeded) -> None:
    """`shared_by_user_id IS NULL` is the discriminator — a real broadcast has an author."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-transitional-downgrade")
    transitional = await make_candidate(admin_session, tenant_id, owner_id=me)
    broadcast = await make_candidate(admin_session, tenant_id, owner_id=me)
    await admin_session.commit()

    module = _revision_module()
    await admin_session.execute(text(module.TRANSITIONAL_SHARE_SQL))
    await admin_session.commit()

    # A recruiter deliberately broadcasts one candidate after deploy. The
    # transitional row is already there, so replace it with an authored one.
    await admin_session.execute(
        text(
            "UPDATE candidate_shares SET shared_by_user_id = :u"
            " WHERE candidate_id = :c AND scope = 'tenant'"
        ),
        {"u": me, "c": broadcast},
    )
    await admin_session.commit()

    await admin_session.execute(text(module.TRANSITIONAL_SHARE_REMOVAL_SQL))
    await admin_session.commit()

    remaining = {
        (r.candidate_id, r.shared_by_user_id)
        for r in (
            await admin_session.execute(
                select(CandidateShare).where(
                    CandidateShare.tenant_id == tenant_id,
                    CandidateShare.scope == CandidateShare.SCOPE_TENANT,
                )
            )
        ).scalars()
    }
    assert remaining == {(broadcast, me)}
    assert transitional not in {c for c, _ in remaining}


@pytest.mark.asyncio
async def test_removal_statement_matches_downgrade(admin_session) -> None:
    """The documented operator command and `downgrade()` must not drift apart."""
    module = _revision_module()
    assert "shared_by_user_id IS NULL" in module.TRANSITIONAL_SHARE_REMOVAL_SQL
    assert module.TRANSITIONAL_SHARE_REMOVAL_SQL.split() == (
        "DELETE FROM candidate_shares WHERE scope = 'tenant'"
        " AND shared_by_user_id IS NULL"
    ).split()
