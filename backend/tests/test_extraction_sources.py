"""An extraction can describe an email or a CV, and must say which.

The "exactly one source" CHECK constraint is declared on the model
(`Extraction.__table_args__`) and, since Task 2's migration, enforced in the
database alongside the FK on `candidate_document_id` — both were
deliberately deferred until `candidate_documents` existed; see the ordering
note in `.superpowers/sdd/task-1-brief.md`.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.models.candidate import CandidateDocument
from app.models.extraction import Extraction
from tests.test_candidate_roles_api import _a_candidate_row, agency  # noqa: F401


@pytest.mark.asyncio
async def test_an_extraction_with_neither_source_is_refused(agency):  # noqa: F811
    """Provenance that names no source is not provenance."""
    tenant_id, _user = agency
    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_id) as session:
            session.add(
                Extraction(
                    tenant_id=tenant_id,
                    email_message_id=None,
                    candidate_document_id=None,
                    model_name="x",
                    prompt_version="v1",
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_an_extraction_with_both_sources_is_refused(agency):  # noqa: F811
    """Provenance that names two sources is not provenance either."""
    tenant_id, _user = agency
    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_id) as session:
            session.add(
                Extraction(
                    tenant_id=tenant_id,
                    email_message_id=uuid.uuid4(),
                    candidate_document_id=uuid.uuid4(),
                    model_name="x",
                    prompt_version="v1",
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_an_extraction_naming_only_a_cv_is_accepted(agency):  # noqa: F811
    """A CV extraction has no email behind it, and that is fine on its own."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    async with tenant_session(tenant_id) as session:
        document = CandidateDocument(
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            filename="cv.pdf",
            content_type="application/pdf",
            byte_size=1234,
            object_key="documents/a.pdf",
            parse_state=CandidateDocument.PENDING,
        )
        session.add(document)
        await session.flush()
        session.add(
            Extraction(
                tenant_id=tenant_id,
                email_message_id=None,
                candidate_document_id=document.id,
                model_name="x",
                prompt_version="v1",
            )
        )
        await session.commit()


def test_the_check_constraint_is_declared_on_the_model():
    """The model states its intent even though the DB does not enforce it yet.

    A later reader diffing the model against the database should find this
    test, not conclude the CHECK was silently forgotten.
    """
    names = {c.name for c in Extraction.__table__.constraints}
    assert "ck_extractions_exactly_one_source" in names
