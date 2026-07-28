"""A CV a candidate came with. Isolation first, then the parse_state whitelist."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.rls import tenant_session
from app.models.candidate import CandidateDocument
from tests.test_candidate_roles_api import _a_candidate_row, agency, other_agency  # noqa: F401


@pytest.mark.asyncio
async def test_a_document_belongs_to_one_tenant_only(agency, other_agency):  # noqa: F811
    """Agency B cannot see Agency A's document even knowing it exists."""
    a_tenant, _a_user = agency
    b_tenant, _b_user = other_agency
    a_candidate = await _a_candidate_row(a_tenant, _a_user)

    async with tenant_session(a_tenant) as session:
        session.add(
            CandidateDocument(
                tenant_id=a_tenant,
                candidate_id=a_candidate,
                filename="cv.pdf",
                content_type="application/pdf",
                byte_size=1234,
                object_key="documents/a.pdf",
                parse_state=CandidateDocument.PENDING,
            )
        )
        await session.commit()

    async with tenant_session(b_tenant) as session:
        rows = (await session.execute(select(CandidateDocument))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_parse_state_rejects_a_value_outside_the_whitelist(agency):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)

    with pytest.raises(DBAPIError):
        async with tenant_session(tenant_id) as session:
            session.add(
                CandidateDocument(
                    tenant_id=tenant_id,
                    candidate_id=candidate_id,
                    filename="cv.pdf",
                    content_type="application/pdf",
                    byte_size=1234,
                    object_key="documents/a.pdf",
                    parse_state="not_a_real_state",
                )
            )
            await session.commit()
