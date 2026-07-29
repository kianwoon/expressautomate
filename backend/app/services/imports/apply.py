"""Matching parsed spreadsheet rows against what is already there, and writing them.

`rows.py` has already turned the uploaded file into typed records; this module
decides which existing person or job each of them is, writes what is safe to
write, and records every field it touched so Task 6 can walk the whole import
back. It writes on the caller's session and never commits: the counts on
`candidate_imports` and the rows written here have to land in one transaction,
or an interrupted import leaves a person half-updated with a tally that says
otherwise.

**Nothing here invents an identity.** Every judgement about who a row refers
to is delegated: `find_candidate` for a person, `match_existing_role` for a
job. Where those refuse to answer, so does this — a refusal becomes a
`RowProblem` naming what collided, and the row is left unwritten rather than
guessed at. Merging two real people is the one failure an undo cannot
meaningfully repair.

**A blank cell is not an erasure.** `rows.py` reports an empty cell as `None`,
and a sheet that simply omits a column is not asserting that the column is
empty. Only a stated value is written, so importing a two-column contact list
cannot wipe everybody's location (§15).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select, text

from app.models.candidate import Candidate, CandidateImportChange, CandidateRole
from app.services.candidate_matching import find_candidate
from app.services.candidate_naming import is_matchable_phone
from app.services.candidate_overrides import overridden_fields
from app.services.client_naming import normalize_company_name
from app.services.cv.persist import match_existing_role
from app.services.imports.rows import CandidateRecord, RoleRecord, RowProblem

SOURCE_IMPORT = "import"

# The sheets these records came off. `rows.py` takes the real sheet name from
# the workbook, but a parsed record does not carry it, so a problem raised
# here names the kind of sheet and locates the row by its position among the
# records handed over — which is not the recruiter's line number when earlier
# rows failed to parse. Task 7 holds both halves and is where the two get
# joined; until then the reason text carries the email or phone, which is what
# a recruiter actually searches their own file by.
_CANDIDATE_SHEET = "candidates"
_ROLE_SHEET = "work history"

# The candidate columns an import is allowed to write, in the order a change
# report reads best. `email` and `phone_e164` are here because a sheet
# genuinely does correct somebody's contact details — but they are also the
# matching keys, so writing one is guarded against colliding with another
# person below.
_CANDIDATE_FIELDS = (
    "full_name",
    "email",
    "phone_raw",
    "phone_e164",
    "current_title",
    "current_employer",
    "location",
)

# The role columns an import writes. `employer_normalized` and
# `title_normalized` are derived rather than read from the sheet, so they are
# updated alongside their raw column rather than listed here.
_ROLE_FIELDS = (
    "employer",
    "title",
    "started_on",
    "started_precision",
    "ended_on",
    "ended_precision",
    "location",
    "description",
)

# A created row has no single field that changed — the whole row is the
# change, and undo deletes it rather than restoring anything. The name records
# that rather than picking one column arbitrarily and implying the others were
# left alone.
_WHOLE_ROW = "*"

_IMPORT_TENANT = text("SELECT tenant_id FROM candidate_imports WHERE id = :id")


@dataclass
class ImportOutcome:
    """What one run of an import did, and what it would not do."""

    candidates_created: int = 0
    candidates_updated: int = 0
    roles_created: int = 0
    roles_updated: int = 0
    problems: list[RowProblem] = field(default_factory=list)


@dataclass
class _Applied:
    """A person this run has already resolved, and the row that resolved them.

    The record is kept beside the id so a second row for the same person can
    be compared against the first. Without it a sheet listing somebody twice
    with different details would silently apply whichever row came last.
    """

    candidate_id: uuid.UUID
    record: CandidateRecord


def _as_text(value) -> str | None:
    """A field value as the change log stores it.

    `previous_value`/`new_value` are text columns covering candidate and role
    fields of several types, so a date lands as its ISO form — the same string
    Postgres would render — and a missing value stays NULL rather than
    becoming the word "None".
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _stated(value) -> bool:
    """Whether the sheet actually said something in this cell."""
    return value is not None and (not isinstance(value, str) or value.strip() != "")


def _keys(email: str | None, phone_e164: str | None) -> list[tuple[str, str]]:
    """The identity keys these details can be recognised by within one run.

    A fixed line is excluded for the same reason `find_candidate` excludes it:
    a switchboard number identifies a company, so keying on one would collapse
    every colleague who listed the office number into a single person.
    """
    keys: list[tuple[str, str]] = []
    if email:
        keys.append(("email", email))
    if is_matchable_phone(phone_e164):
        keys.append(("phone", phone_e164))
    return keys


def _disagreement(first: CandidateRecord, second: CandidateRecord) -> str | None:
    """The first field on which two rows for one person contradict each other.

    Only fields both rows state are compared: a second row that leaves a
    column blank is not contradicting the first, it is saying nothing.
    """
    for name in _CANDIDATE_FIELDS:
        a, b = getattr(first, name), getattr(second, name)
        if _stated(a) and _stated(b) and a != b:
            return f"{name} {a!r} then {b!r}"
    return None


async def _import_tenant(session, import_id: uuid.UUID) -> uuid.UUID:
    """The tenant that owns this import, refusing anything that is not ours.

    `Candidate.import_id` and `CandidateRole.import_id` are plain foreign
    keys, not the composite `(tenant_id, id)` idiom the rest of the schema
    uses — a composite FK's bare `ON DELETE SET NULL` would null `tenant_id`
    too, and that column is NOT NULL. The cost of that necessary compromise is
    that **nothing at the database level stops `import_id` from pointing at
    another tenant's import**, so this check is the only thing that does.
    Under RLS a foreign import is simply invisible, which lands here as a
    missing row; an unscoped session would still see it and be refused by the
    comparison.
    """
    row = (await session.execute(_IMPORT_TENANT, {"id": import_id})).first()
    if row is None:
        raise ValueError(f"import {import_id} does not exist for this tenant")
    return row.tenant_id


async def _holder(
    session, column: str, value: str, exclude: uuid.UUID | None
) -> uuid.UUID | None:
    """Whoever else in this tenant already holds this email or phone.

    Checked before an identity key is written rather than letting the unique
    index raise: a `IntegrityError` mid-run aborts the whole transaction, so
    one recruiter's typo in row 400 would cost the other 499 rows. The tenant
    predicate is RLS's, implicit on the ORM query.
    """
    statement = select(Candidate.id).where(
        Candidate.record_status != Candidate.MERGED
    )
    if column == "email":
        # `lower(...)`, not `ILIKE`: the unique index is on `lower(email)`, and
        # `ILIKE` would read `%` in an address as a wildcard.
        statement = statement.where(func.lower(Candidate.email) == value.lower())
    else:
        statement = statement.where(Candidate.phone_e164 == value)
    if exclude is not None:
        statement = statement.where(Candidate.id != exclude)
    return (await session.execute(statement.limit(1))).scalars().first()


def _change(
    tenant_id: uuid.UUID,
    import_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    field_name: str,
    previous,
    new,
) -> CandidateImportChange:
    return CandidateImportChange(
        tenant_id=tenant_id,
        import_id=import_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        field_name=field_name,
        previous_value=_as_text(previous),
        new_value=_as_text(new),
    )


async def _update_candidate(
    session,
    *,
    tenant_id: uuid.UUID,
    import_id: uuid.UUID,
    candidate: Candidate,
    record: CandidateRecord,
    problems: list[RowProblem],
    line: int,
) -> bool:
    """Write the sheet onto an existing person. True if anything moved.

    The import wins on every field except one a human asserted: a
    `CandidateFieldOverride` is somebody typing over a value on purpose, and
    an older sheet re-uploaded next month must not undo that. Each field that
    does change is logged *before* it is written, with both its previous and
    its new value — undo restores a field only when the current value still
    equals what the import wrote, and that comparison is undecidable without
    the second half.
    """
    protected = await overridden_fields(session, candidate.id)
    changed = False

    for name in _CANDIDATE_FIELDS:
        new = getattr(record, name)
        if not _stated(new) or name in protected:
            continue
        previous = getattr(candidate, name)
        if previous == new:
            continue
        if name in ("email", "phone_e164"):
            other = await _holder(session, name, new, exclude=candidate.id)
            if other is not None:
                problems.append(
                    RowProblem(
                        sheet=_CANDIDATE_SHEET,
                        line=line,
                        reason=(
                            f"{name} {new!r} already belongs to candidate {other}, "
                            f"so it was left off {candidate.id}"
                        ),
                    )
                )
                continue
        session.add(
            _change(
                tenant_id,
                import_id,
                CandidateImportChange.CANDIDATE,
                candidate.id,
                CandidateImportChange.UPDATED,
                name,
                previous,
                new,
            )
        )
        setattr(candidate, name, new)
        changed = True

    return changed


async def _apply_candidates(
    session,
    *,
    tenant_id: uuid.UUID,
    import_id: uuid.UUID,
    records: list[CandidateRecord],
    outcome: ImportOutcome,
) -> dict[tuple[str, str], _Applied]:
    """Every candidate row, matched or created, before a single role is read."""
    seen: dict[tuple[str, str], _Applied] = {}

    for index, record in enumerate(records):
        line = index + 2
        keys = _keys(record.email, record.phone_e164)

        # The run's own map first. A file listing somebody twice describes one
        # person, and `find_candidate` cannot know that on the second row
        # because the first row's write has not been flushed yet.
        already = next((seen[key] for key in keys if key in seen), None)
        if already is not None:
            clash = _disagreement(already.record, record)
            if clash is not None:
                outcome.problems.append(
                    RowProblem(
                        sheet=_CANDIDATE_SHEET,
                        line=line,
                        reason=(
                            f"this file lists {record.email or record.phone_e164} twice "
                            f"with different details ({clash}); the later row was not "
                            "applied"
                        ),
                    )
                )
            continue

        match = await find_candidate(session, tenant_id, record.email, record.phone_e164)
        if match.conflict is not None:
            # Two keys, two different people. Picking one would attach this
            # row's details to the wrong person and creating a third would
            # duplicate both; a duplicate somebody merges later is the lesser
            # harm this codebase has already chosen, and it needs a human.
            left, right = match.conflict
            outcome.problems.append(
                RowProblem(
                    sheet=_CANDIDATE_SHEET,
                    line=line,
                    reason=(
                        f"email and phone point at two different people, {left} and "
                        f"{right}; nothing was written for this row"
                    ),
                )
            )
            continue

        if match.candidate_id is not None:
            candidate = (
                await session.execute(
                    select(Candidate).where(Candidate.id == match.candidate_id)
                )
            ).scalars().one()
            if await _update_candidate(
                session,
                tenant_id=tenant_id,
                import_id=import_id,
                candidate=candidate,
                record=record,
                problems=outcome.problems,
                line=line,
            ):
                outcome.candidates_updated += 1
        else:
            candidate = Candidate(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                full_name=record.full_name,
                email=record.email,
                phone_raw=record.phone_raw,
                phone_e164=record.phone_e164,
                current_title=record.current_title,
                current_employer=record.current_employer,
                location=record.location,
                # Only a created row carries the import. An update leaves it
                # alone deliberately: overwriting it would be a change with no
                # change row behind it, and undoing an import would then have
                # to restore a pointer it never recorded.
                import_id=import_id,
            )
            session.add(candidate)
            session.add(
                _change(
                    tenant_id,
                    import_id,
                    CandidateImportChange.CANDIDATE,
                    candidate.id,
                    CandidateImportChange.CREATED,
                    _WHOLE_ROW,
                    None,
                    None,
                )
            )
            outcome.candidates_created += 1

        for key in keys:
            seen[key] = _Applied(candidate_id=candidate.id, record=record)

    return seen


def _dates(record: RoleRecord) -> tuple[date | None, str | None, date | None, str | None]:
    """The role's dates, keeping only what the sheet could honestly be read as.

    A precision of `None` is `rows.py` reporting a cell it could not read; the
    date that came with it is not a fact, so it is dropped and the job kept —
    the same call `cv/persist.py:stored_date` makes for a half-read date. An
    end before a start is dropped whole, because the check constraint refuses
    it and a recruiter's transposed cells are not a reason to fail the run.
    """
    started_on = record.started_on if record.started_precision else None
    ended_on = record.ended_on if record.ended_precision else None
    started_precision = record.started_precision if started_on else None
    ended_precision = record.ended_precision if ended_on else None
    if started_on and ended_on and ended_on < started_on:
        return None, None, None, None
    return started_on, started_precision, ended_on, ended_precision


async def _existing_roles(session, candidate_id: uuid.UUID) -> list[CandidateRole]:
    return list(
        (
            await session.execute(
                select(CandidateRole).where(CandidateRole.candidate_id == candidate_id)
            )
        )
        .scalars()
        .all()
    )


async def _candidate_for(
    session,
    *,
    tenant_id: uuid.UUID,
    record: RoleRecord,
    seen: dict[tuple[str, str], _Applied],
    problems: list[RowProblem],
    line: int,
) -> uuid.UUID | None:
    """The person this job belongs to, or a reported problem and nobody.

    A history row carries an employer and a title but no name, so there is
    nothing to create a person *from*: inventing one would produce a record
    with a job and no human attached to it. An unmatched row is therefore
    reported and skipped, never turned into a candidate.
    """
    keys = _keys(record.candidate_email, record.candidate_phone)
    applied = next((seen[key] for key in keys if key in seen), None)
    if applied is not None:
        return applied.candidate_id

    match = await find_candidate(
        session, tenant_id, record.candidate_email, record.candidate_phone
    )
    if match.conflict is not None:
        left, right = match.conflict
        problems.append(
            RowProblem(
                sheet=_ROLE_SHEET,
                line=line,
                reason=(
                    f"email and phone point at two different people, {left} and "
                    f"{right}; this job was not attached to either"
                ),
            )
        )
        return None
    if match.candidate_id is None:
        who = record.candidate_email or record.candidate_phone
        problems.append(
            RowProblem(
                sheet=_ROLE_SHEET,
                line=line,
                reason=(
                    f"no candidate in this file or already on record matches {who}, "
                    "and a job on its own does not name a person"
                ),
            )
        )
        return None
    return match.candidate_id


async def _apply_roles(
    session,
    *,
    tenant_id: uuid.UUID,
    import_id: uuid.UUID,
    records: list[RoleRecord],
    seen: dict[tuple[str, str], _Applied],
    today: date,
    outcome: ImportOutcome,
) -> None:
    cache: dict[uuid.UUID, list[CandidateRole]] = {}

    for index, record in enumerate(records):
        line = index + 2
        candidate_id = await _candidate_for(
            session,
            tenant_id=tenant_id,
            record=record,
            seen=seen,
            problems=outcome.problems,
            line=line,
        )
        if candidate_id is None:
            continue

        employer = record.employer.strip()
        title = record.title.strip()
        if not employer or not title:
            # `candidate_roles` requires both, and deriving one from the other
            # is exactly the fabrication §15 forbids — `cv/persist.py` drops
            # such a role for the same reason.
            missing = "employer" if not employer else "title"
            outcome.problems.append(
                RowProblem(
                    sheet=_ROLE_SHEET,
                    line=line,
                    reason=f"this job has no {missing}, so it cannot be recorded",
                )
            )
            continue

        employer_normalized = normalize_company_name(employer)
        started_on, started_precision, ended_on, ended_precision = _dates(record)

        if candidate_id not in cache:
            cache[candidate_id] = await _existing_roles(session, candidate_id)
        existing = cache[candidate_id]

        match = match_existing_role(
            employer_normalized,
            started_on,
            started_precision,
            ended_on,
            ended_precision,
            existing,
            today,
        )
        values = {
            "employer": employer,
            "title": title,
            "started_on": started_on,
            "started_precision": started_precision,
            "ended_on": ended_on,
            "ended_precision": ended_precision,
            "location": record.location,
            "description": record.description,
        }

        if match is not None:
            role = next(row for row in existing if row.id == match)
            if role.status == CandidateRole.REJECTED:
                # A recruiter looked at this job and threw it out. `cv/persist`
                # keeps such a row exactly as rejected rather than letting a
                # later source reopen it, and a spreadsheet is no more entitled
                # to overrule that than a CV is — but silently dropping the row
                # would leave the recruiter believing it landed, so it is
                # reported instead.
                outcome.problems.append(
                    RowProblem(
                        sheet=_ROLE_SHEET,
                        line=line,
                        reason=(
                            f"this job matches one already rejected on candidate "
                            f"{candidate_id}, which an import does not reopen"
                        ),
                    )
                )
                continue

            changed = False
            for name, new in values.items():
                previous = getattr(role, name)
                if previous == new:
                    continue
                session.add(
                    _change(
                        tenant_id,
                        import_id,
                        CandidateImportChange.ROLE,
                        role.id,
                        CandidateImportChange.UPDATED,
                        name,
                        previous,
                        new,
                    )
                )
                setattr(role, name, new)
                changed = True
            # The provenance columns travel with the values rather than being
            # diffed as fields of their own: a row the sheet just corrected
            # came from the import, and a spreadsheet is the agency's own
            # record rather than a proposal awaiting review.
            for name, new in (("source", SOURCE_IMPORT), ("status", CandidateRole.CONFIRMED)):
                previous = getattr(role, name)
                if previous == new:
                    continue
                session.add(
                    _change(
                        tenant_id,
                        import_id,
                        CandidateImportChange.ROLE,
                        role.id,
                        CandidateImportChange.UPDATED,
                        name,
                        previous,
                        new,
                    )
                )
                setattr(role, name, new)
                changed = True
            if changed:
                role.employer_normalized = employer_normalized
                role.title_normalized = title.strip().lower()
                outcome.roles_updated += 1
            continue

        role = CandidateRole(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            employer_normalized=employer_normalized,
            # The plainest normalisation there is, matching what the roles API
            # does when a person types one.
            title_normalized=title.lower(),
            source=SOURCE_IMPORT,
            status=CandidateRole.CONFIRMED,
            import_id=import_id,
            **values,
        )
        session.add(role)
        session.add(
            _change(
                tenant_id,
                import_id,
                CandidateImportChange.ROLE,
                role.id,
                CandidateImportChange.CREATED,
                _WHOLE_ROW,
                None,
                None,
            )
        )
        # So a file listing the same employer twice for one person cannot
        # match its own first row on the second pass and collapse two real
        # positions into one — the same guard `cv/persist.py` keeps.
        existing.append(role)
        outcome.roles_created += 1


async def apply_import(
    session,
    *,
    tenant_id: uuid.UUID,
    import_id: uuid.UUID,
    candidates: list[CandidateRecord],
    roles: list[RoleRecord],
    today: date,
) -> ImportOutcome:
    """Apply one parsed spreadsheet, and report what it did and would not do.

    Candidates are applied and flushed before any history row is looked at, so
    a job can attach to somebody created moments earlier in the same file.
    Any other order makes a single-file migration — the ordinary case, a
    roster and a career history in one workbook — impossible.
    """
    owner = await _import_tenant(session, import_id)
    if owner != tenant_id:
        raise ValueError(f"import {import_id} belongs to tenant {owner}, not {tenant_id}")

    outcome = ImportOutcome()
    seen = await _apply_candidates(
        session,
        tenant_id=tenant_id,
        import_id=import_id,
        records=candidates,
        outcome=outcome,
    )
    await session.flush()
    await _apply_roles(
        session,
        tenant_id=tenant_id,
        import_id=import_id,
        records=roles,
        seen=seen,
        today=today,
        outcome=outcome,
    )
    await session.flush()
    return outcome
