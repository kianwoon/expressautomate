"""Two recruiters, one person, two readings of them.

The base row holds facts. Judgement — salary expectation, seniority,
availability — is attributed to the recruiter who formed it.
"""

import uuid

import pytest
from sqlalchemy import text

from app.models.candidate import CandidateFieldOverride
from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_two_recruiters_hold_different_values_for_one_field(
    admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-two-readings")
    second = await make_user(admin_session, tenant_id, "second@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=first)

    for user_id, value in ((first, "9000"), (second, "8000")):
        admin_session.add(
            CandidateFieldOverride(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                user_id=user_id,
                field_name="salary_expectation",
                human_value=value,
                changed_by=user_id,
            )
        )
    # Committed, not merely flushed: `seeded`'s teardown DELETEs these rows
    # from another connection, and an open transaction holding row locks
    # deadlocks it. (The IntegrityError test below can flush, because a failed
    # flush rolls the transaction back and releases them.)
    await admin_session.commit()  # must not raise: the key includes user_id

    rows = (
        await admin_session.execute(
            text(
                "SELECT human_value FROM candidate_field_overrides "
                "WHERE candidate_id = :c ORDER BY human_value"
            ),
            {"c": candidate_id},
        )
    ).all()
    assert [r.human_value for r in rows] == ["8000", "9000"]


@pytest.mark.asyncio
async def test_the_tenant_wide_tier_survives_and_stays_singular(
    admin_session, seeded
) -> None:
    """`user_id IS NULL` is a distinct, permanent tier — not a missing value.

    Every override written before this change was import protection for the
    whole agency. Backfilling them to `changed_by` would have made them one
    person's private opinion and let the next import overwrite the field for
    everyone else.

    And a NULL does not collide with another NULL in a Postgres UNIQUE
    constraint, so without a second partial index there could be two
    agency-wide overrides on one field.
    """
    from sqlalchemy.exc import IntegrityError

    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-tenant-tier")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=first)

    for _ in range(2):
        admin_session.add(
            CandidateFieldOverride(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                user_id=None,
                field_name="current_title",
                human_value="Tech Lead",
                changed_by=first,
            )
        )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_rendering_reads_the_null_tier_plus_the_callers(
    admin_session, seeded
) -> None:
    from app.services.candidate_overrides import overridden_fields

    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-render-tiers")
    second = await make_user(admin_session, tenant_id, "render2@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=first)
    for user_id, field in ((None, "current_title"), (first, "salary_expectation")):
        admin_session.add(
            CandidateFieldOverride(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                user_id=user_id,
                field_name=field,
                human_value="x",
                changed_by=first,
            )
        )
    await admin_session.commit()  # see the note in the first test

    assert await overridden_fields(admin_session, candidate_id, first) == {
        "current_title",
        "salary_expectation",
    }
    # The second recruiter sees the agency-wide tier and their own — not the
    # first recruiter's private reading.
    assert await overridden_fields(admin_session, candidate_id, second) == {
        "current_title"
    }


def test_every_candidate_column_is_classified() -> None:
    """A new column must be declared fact or judgement, deliberately.

    The failure mode this prevents is silent: an unclassified field defaults
    to whichever branch the code happens to take, and nobody finds out until
    two recruiters disagree about it in production.
    """
    from app.models.candidate import Candidate
    from app.services.candidate_overrides import JUDGEMENT_FIELDS, SHARED_FACT_FIELDS

    columns = {c.name for c in Candidate.__table__.columns} - {
        "id",
        "tenant_id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "owner_id",
        "import_id",
        "merged_into_candidate_id",
        "record_status",
        "pipeline_stage",
    }
    unclassified = columns - JUDGEMENT_FIELDS - SHARED_FACT_FIELDS
    assert unclassified == set(), f"classify these as fact or judgement: {unclassified}"
