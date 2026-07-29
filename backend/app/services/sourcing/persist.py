"""Loading what a run scores, and keeping what it found.

The database half of a sourcing run, kept out of the job so the job reads as
the sequence of steps it is. Three concerns live here.

**One query per kind, not one per candidate.** `eligible_candidates` returns
ids, and an agency's whole active roster can be behind them. Fetching a
candidate's roles and skills one candidate at a time turns a single run into
two thousand round trips, which is slow enough on a local database and
ruinous across a network — so the roles and skills for every id come back in
one `IN (...)` each, and are grouped in memory.

**A `Component` is not JSON.** `weight`, `raw` and `contribution` are
`Decimal`s, and `json.dumps` refuses a `Decimal` outright — writing the
dataclasses straight into `sourcing_matches.reasons` throws at insert rather
than storing something wrong, which is the good version of this bug and still
a run that never completes. `serialize_components` renders them as strings:
`str` rather than `float` because these numbers are the arithmetic behind a
score a recruiter may query, and a float round-trip would show them
`0.6499999999999999` for a value the scorer computed exactly.

**A stored run is a record, not a query.** `record_matches` writes the score
and the reasons as computed, and `read_matches` reads them back untouched —
so a candidate who changes employer next week does not retroactively change
what a recruiter was shown last week. The read order is
`score DESC, candidate_id` rather than `score DESC` alone: two candidates can
genuinely tie, and without the second key the pair swaps places between two
readings of the same rows.
"""

import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate, CandidateDocument, CandidateRole, CandidateSkill
from app.models.sourcing import SourcingMatch
from app.services.sourcing.score import Component


def serialize_components(components: list[Component]) -> list[dict]:
    """A component list as JSONB will accept it.

    `None` survives as `null` — the distinction between "scored zero" and
    "nothing to compare" is the one rule `score.py` is built around, and
    flattening it here would put "a poor fit on salary" in front of a
    recruiter when the truth is that nobody recorded a salary.
    """
    return [
        {
            "name": component.name,
            "weight": _number(component.weight),
            "raw": _number(component.raw),
            "contribution": _number(component.contribution),
            "note": component.note,
        }
        for component in components
    ]


def _number(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


async def load_scoring_inputs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[Candidate, list[CandidateRole], list[CandidateSkill]]]:
    """Every candidate the run will score, with their roles and skills.

    Three queries in total regardless of how many candidates there are. The
    `tenant_id` predicate is stated as well as enforced by RLS, the same
    belt-and-braces `eligible.py` explains: the policy is the boundary, this
    makes the query say out loud which agency it is for.
    """
    if not candidate_ids:
        return {}

    candidates = (
        await session.execute(
            select(Candidate).where(
                Candidate.tenant_id == tenant_id, Candidate.id.in_(candidate_ids)
            )
        )
    ).scalars()

    roles: dict[uuid.UUID, list[CandidateRole]] = defaultdict(list)
    for role in (
        await session.execute(
            select(CandidateRole).where(
                CandidateRole.tenant_id == tenant_id,
                CandidateRole.candidate_id.in_(candidate_ids),
            )
        )
    ).scalars():
        roles[role.candidate_id].append(role)

    skills: dict[uuid.UUID, list[CandidateSkill]] = defaultdict(list)
    for skill in (
        await session.execute(
            select(CandidateSkill).where(
                CandidateSkill.tenant_id == tenant_id,
                CandidateSkill.candidate_id.in_(candidate_ids),
            )
        )
    ).scalars():
        skills[skill.candidate_id].append(skill)

    return {c.id: (c, roles[c.id], skills[c.id]) for c in candidates}


async def parsed_text_keys(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_ids: list[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Where each candidate's extracted CV text lives, for the ones that have one.

    Only the shortlist is passed here, not the whole roster: the text is what
    an explanation is checked against, and only the shortlist is explained.
    Newest document wins where somebody has uploaded more than one, because
    the most recent CV is the one that describes the person applying now.
    """
    if not candidate_ids:
        return {}

    rows = (
        await session.execute(
            select(
                CandidateDocument.candidate_id,
                CandidateDocument.text_key,
                CandidateDocument.created_at,
            )
            .where(
                CandidateDocument.tenant_id == tenant_id,
                CandidateDocument.candidate_id.in_(candidate_ids),
                CandidateDocument.parse_state == CandidateDocument.PARSED,
                CandidateDocument.text_key.is_not(None),
            )
            .order_by(CandidateDocument.candidate_id, CandidateDocument.created_at)
        )
    ).all()
    # Ascending order with a plain overwrite leaves the newest in place.
    return {row.candidate_id: row.text_key for row in rows}


async def record_matches(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    matches: list[dict],
) -> int:
    """Write a run's matches. Returns how many rows were written.

    No upsert and no delete-first: a run is claimed by exactly one worker
    through the conditional UPDATE in `run_sourcing`, so a second writer here
    would be a bug the unique key should surface rather than a case to
    absorb.
    """
    for match in matches:
        session.add(
            SourcingMatch(
                tenant_id=tenant_id,
                run_id=run_id,
                candidate_id=match["candidate_id"],
                score=match["score"],
                reasons=match["reasons"],
                explanation=match.get("explanation"),
                explanation_evidence=match.get("explanation_evidence"),
            )
        )
    return len(matches)


async def read_matches(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> list[SourcingMatch]:
    """A run's matches, best first, and the same way every time.

    `candidate_id` breaks the tie for the reason given at the top of this
    module: an equal-scoring pair with no second key comes back in whatever
    order the plan produced, so the same stored run would present two
    different shortlists to two people reading it.
    """
    return list(
        (
            await session.execute(
                select(SourcingMatch)
                .where(
                    SourcingMatch.tenant_id == tenant_id,
                    SourcingMatch.run_id == run_id,
                )
                .order_by(SourcingMatch.score.desc(), SourcingMatch.candidate_id)
            )
        ).scalars()
    )
