"""The fields on a candidate a person asserted, which no machine may overwrite.

One question, asked from three directions: the roles API re-deriving a profile,
the candidate GET explaining to a recruiter why an import left a field alone,
and the spreadsheet importer deciding what it is allowed to write. It lived in
`app.api.candidate_roles` while the API was its only caller; a service reaching
back into an API module is the wrong direction of dependency and would have
closed a cycle the moment the importer needed it, so the rule lives here and
the API imports it like everyone else.
"""

import uuid

from sqlalchemy import select

from app.models.candidate import CandidateFieldOverride


async def overridden_fields(session, candidate_id: uuid.UUID) -> set[str]:
    """The fields a person asserted, which derivation must leave alone."""
    return set(
        (
            await session.execute(
                select(CandidateFieldOverride.field_name).where(
                    CandidateFieldOverride.candidate_id == candidate_id
                )
            )
        ).scalars()
    )
