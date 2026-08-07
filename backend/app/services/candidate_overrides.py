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

from sqlalchemy import or_, select

from app.models.candidate import CandidateFieldOverride

# The fact/judgement line, drawn once and enforced by
# `tests/test_candidate_overrides_per_user.py::test_every_candidate_column_is_classified`
# rather than maintained in prose. A column added next month fails that test
# until somebody decides which side it is on.
#
# The rule: a fact is one value about a person that does not depend on who is
# looking — identity, contact details, documents, demographic and legal status.
# Judgement is one recruiter's reading, formed in a conversation the other
# recruiter was not in, and two honest recruiters can disagree about it.
#
# Where a field is genuinely arguable it goes in SHARED_FACT_FIELDS: a shared
# value that later needs splitting is a smaller mistake than a private value
# nobody else can see.
SHARED_FACT_FIELDS: frozenset[str] = frozenset(
    {
        # Identity and contact.
        "full_name",
        "email",
        "phone_raw",
        "phone_e164",
        "location",
        # Demographic and legal status. Not a reading of the person — either
        # recorded from a document or stated by them, and wrong rather than
        # merely different when two recruiters disagree.
        "sex",
        "race",
        "race_detail",
        "nationality",
        "date_of_birth",
        # Arguable, deliberately shared. `current_title` and `current_employer`
        # are derived from the role history, which is itself shared; a private
        # reading of them would drift from the roles rendered beside them.
        "current_title",
        "current_employer",
        # Arguable, deliberately shared: both are counted off the same role
        # history and education record every recruiter can see.
        "years_experience",
        "education_years",
        # Quoted from the CV, so a shared fact the candidate stated — not a
        # recruiter's private reading. A separate trio from `expected_salary`
        # (which is judgement) because current and expected can be heard in
        # different conversations and stated in different units.
        "last_drawn_salary",
        "last_drawn_currency",
        "last_drawn_period",
        # The avatar is one image of one person.
        "avatar_key",
        "avatar_updated_at",
    }
)

# What one recruiter heard in one conversation. The candidate who told the
# first recruiter $9k and the second $8k six weeks later was not lying to
# either of them, and the system must be able to hold both.
JUDGEMENT_FIELDS: frozenset[str] = frozenset(
    {
        "expected_salary",
        "salary_currency",
        "salary_period",
        "available_from",
        "notice_period_raw",
        "employment_type",
        "notes",
    }
)


async def overridden_fields(
    session, candidate_id: uuid.UUID, user_id: uuid.UUID | None
) -> set[str]:
    """Which fields carry a human value, for THIS reader.

    Two tiers, ORed: `user_id IS NULL` is agency-wide — a fact somebody
    corrected for everybody, and the meaning every row written before
    candidates had owners carries. A row naming a user is that recruiter's own
    reading of a judgement field, and nobody else's business.
    """
    return set(
        (
            await session.execute(
                select(CandidateFieldOverride.field_name)
                .where(CandidateFieldOverride.candidate_id == candidate_id)
                .where(
                    or_(
                        CandidateFieldOverride.user_id.is_(None),
                        CandidateFieldOverride.user_id == user_id,
                    )
                )
            )
        ).scalars()
    )
