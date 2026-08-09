"""Taking an import back, without taking back anything that is no longer its doing.

An import writes straight to live data with no preview stage, and that is only
defensible because it can be reversed. A mis-mapped column rewrites hundreds of
candidate records in one pass, and this module is the whole answer to it.

**One rule carries the module: a field is restored only if its current value
still equals `new_value`** — what the import actually wrote. If a recruiter
retyped it afterwards, their edit is newer and better informed, and an undo
reaching past it would destroy exactly the data somebody cared enough to fix.
That single comparison is also what makes undo repeatable: a second pass finds
nothing still matching what the first pass already put back, so it changes
nothing rather than winding the value back a further step.

Rows the import *created* are deleted outright — they exist only because of it,
so there is no earlier state to return them to. That is keyed on
`action == created`; `field_name` on such a row is the `"*"` sentinel
`apply.py` writes to record that the whole row was the change, and it is
informational only. A created candidate is the one exception: if any human
work has landed on it since — a role, an edited field, an uploaded document,
a skill, a move through the pipeline — deleting the candidate cascades that
work away too, and no change record exists to put any of it back. Such a
candidate is kept and the skip reported, the same protection the restore rule
above gives an updated field.

Whatever undo would not do is reported rather than swallowed. An undo that
reversed less than the whole import must say so — a caller that presented a
partial reversal as a clean one would be telling the recruiter their data is
back when some of it is not.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect, select, update

from app.models.candidate import (
    Candidate,
    CandidateDocument,
    CandidateFieldOverride,
    CandidateImport,
    CandidateImportChange,
    CandidateRole,
    CandidateSkill,
)

# Deliberately imported rather than re-implemented. `_as_text` is how the
# change log rendered every value on the way in, so it is the only rendering
# that can be compared against `new_value` on the way back. Two independent
# copies that drifted apart would make undo silently inert — every comparison
# failing, every field "skipped", and nothing about the result looking wrong.
# `_holder` is the same rule apply.py uses before writing an identity key, so
# the reversal does not collide with a key somebody else has taken since.
from app.services.imports.apply import _as_text, _holder

# The mapped class behind each `entity_type`. Undo reads the column types off
# these to put a text value back as whatever the column actually holds.
_MODELS = {
    CandidateImportChange.CANDIDATE: Candidate,
    CandidateImportChange.ROLE: CandidateRole,
}

# The states an import may be undone from: exactly the complement of
# `import_jobs._RESUMABLE`, which is the point. These are the states no
# worker will pick the row up from again, so nothing can apply the file
# behind a reversal. `pending` and `parsing` are absent — undoing from
# either races the worker, and `pending` is the one that looks safe and is
# not, because the job can claim it a microsecond later. `undone` is present
# so a second pass stays the harmless no-op the restore rule already makes
# it.
SETTLED = (CandidateImport.DONE, CandidateImport.FAILED, CandidateImport.UNDONE)

# The inverse of `_as_text`, one entry per Python type a mapped column can
# report. Keyed on the column's own `python_type` rather than on field names,
# so a column added to the import later is coerced correctly without anyone
# remembering to list it here.
_FROM_TEXT = {
    date: date.fromisoformat,
    datetime: datetime.fromisoformat,
    int: int,
    float: float,
    Decimal: Decimal,
    bool: lambda value: value == str(True),
    str: str,
}


@dataclass(frozen=True)
class UndoSkip:
    """One field undo left alone, and why it did."""

    entity_type: str
    entity_id: uuid.UUID
    field_name: str
    reason: str


@dataclass
class UndoOutcome:
    """What the reversal put back, and what it would not touch."""

    rows_deleted: int = 0
    fields_restored: int = 0
    fields_skipped: int = 0
    skips: list[UndoSkip] = field(default_factory=list)


def _restored(model, field_name: str, value: str | None):
    """A logged `previous_value` as the column it goes back into holds it.

    The change log is text across two tables of several column types, so a
    date left it as an ISO string and has to come back a `date` — assigning
    the string would either raise on flush or, worse, land as a value the
    column silently coerced differently. A NULL stays NULL: it is the honest
    record that the field had no value before the import, not an empty string.
    """
    if value is None:
        return None
    column = inspect(model).columns[field_name]
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        # A column type that will not name a Python type has nothing to coerce
        # to, so the stored text is the best available answer.
        return value
    return _FROM_TEXT.get(python_type, str)(value)


async def _the_import(session, tenant_id: uuid.UUID, import_id: uuid.UUID) -> CandidateImport:
    """The import to reverse, refusing anything that is not this agency's.

    `Candidate.import_id` and `CandidateRole.import_id` are plain foreign keys
    rather than the composite `(tenant_id, id)` idiom the rest of the schema
    uses, because a composite's bare `ON DELETE SET NULL` would null
    `tenant_id` too and that column is NOT NULL. The price is that **nothing
    at the database level stops an `import_id` pointing at another tenant's
    import**. `apply.py` asserts ownership on the way in; this is the matching
    assertion on the way back, and without it an undo would be a way to delete
    another agency's candidates. Under RLS a foreign import is invisible and
    lands here as a missing row; an unscoped session would still see it and be
    refused by the comparison below.
    """
    record = (
        (await session.execute(select(CandidateImport).where(CandidateImport.id == import_id)))
        .scalars()
        .first()
    )
    if record is None:
        raise ValueError(f"import {import_id} does not exist for this tenant")
    if record.tenant_id != tenant_id:
        raise ValueError(
            f"import {import_id} belongs to tenant {record.tenant_id}, not {tenant_id}"
        )
    return record


def _and(phrases: list[str]) -> str:
    """The evidence phrases as one clause a recruiter can read aloud."""
    if len(phrases) == 1:
        return phrases[0]
    return f"{', '.join(phrases[:-1])} and {phrases[-1]}"


async def _later_human_work(
    session, import_id: uuid.UUID, candidates: list
) -> dict[uuid.UUID, list[str]]:
    """Every sign that a person has worked on these candidates since the import.

    Deleting a created candidate cascades: roles, skills, documents and
    overrides all go with the row, and none of them is in the change log, so
    none of them can be put back. The keep-check therefore has to cover every
    kind of record a human can attach, not just the one that was noticed
    first — the narrower rule quietly destroyed a recruiter's corrections, an
    uploaded CV and a hand-entered skill while the confirmation dialog
    promised their work was safe. An uploaded CV is the worst of them: the
    file stays in R2 and the row naming it is gone, so nothing can ever find
    the object again.

    The four attached kinds are counted in the database rather than fetched,
    because this runs on a whole import's worth of created candidates and the
    only question is whether the count is zero. A role is only evidence if it
    is *not* this import's own — those are deleted a moment earlier — and a
    NULL `import_id` (the import record was deleted since) counts as foreign,
    which is why the comparison is spelled out rather than left to SQL's
    NULL-propagating `<>`.

    The candidate's own row is read too. `pipeline_stage` and `record_status`
    are the two columns an import never writes, so anything other than the
    values a created row starts at is somebody having moved this person
    through the process — work that no cascade touches, but which deleting the
    row destroys just as thoroughly.
    """
    ids = [row.id for row in candidates]
    found: dict[uuid.UUID, list[str]] = {}

    def note(candidate_id: uuid.UUID, phrase: str) -> None:
        found.setdefault(candidate_id, []).append(phrase)

    roles = (
        (
            await session.execute(
                select(CandidateRole.candidate_id, CandidateRole.import_id).where(
                    CandidateRole.candidate_id.in_(ids)
                )
            )
        )
        .all()
    )
    foreign: dict[uuid.UUID, int] = {}
    for candidate_id, role_import in roles:
        if role_import != import_id:
            foreign[candidate_id] = foreign.get(candidate_id, 0) + 1
    for candidate_id, count in foreign.items():
        note(candidate_id, f"{count} role(s) were added to it")

    for model, phrase in (
        (CandidateFieldOverride, "field(s) on it were edited by hand"),
        (CandidateDocument, "document(s) were uploaded to it"),
        (CandidateSkill, "skill(s) were added to it"),
    ):
        rows = (
            await session.execute(
                select(model.candidate_id).where(model.candidate_id.in_(ids))
            )
        ).scalars().all()
        counted: dict[uuid.UUID, int] = {}
        for candidate_id in rows:
            counted[candidate_id] = counted.get(candidate_id, 0) + 1
        for candidate_id, count in counted.items():
            note(candidate_id, f"{count} {phrase}")

    for row in candidates:
        if row.pipeline_stage != Candidate.STAGES[0]:
            note(row.id, f"it was moved to the {row.pipeline_stage} stage")
        if row.record_status != Candidate.ACTIVE:
            note(row.id, f"it was marked {row.record_status}")

    return found


async def _delete_created(
    session, import_id: uuid.UUID, changes: list[CandidateImportChange], outcome: UndoOutcome
) -> None:
    """Remove every row this import brought into existence.

    Roles go first, and the order is the difference between an honest count
    and a misleading one. Deleting the candidate first takes its roles with it
    on `fk_candidate_roles_candidate_same_tenant`'s cascade, and the roles are
    then no longer there to be found — so a run that removed a person and
    their job would report one row deleted, understating what it did to the
    recruiter reading the confirmation. Deleting a role never removes a
    candidate, so this direction has no such shadow.

    A created candidate is the one place undo can do worse than nothing: the
    candidate itself has no earlier state to protect, but the cascade that
    removes it also removes everything now hanging off it — a role a recruiter
    typed by hand, a field they corrected, the CV they uploaded, the skills
    they tagged — none of which this module has any record of, and so no way
    to put back. `_later_human_work` is the whole keep-check; roles this
    import itself created are deleted first, above, so whatever is still
    attached by the time we get here is work nobody logged as the import's
    doing. `CandidateRole.import_id` is used to tell those two apart rather
    than the change log, because it is a plain column set once at row creation
    — reading it needs no correlation between a role's change record and the
    candidate it belongs to, and it stays correct even if the log's retention
    window has already dropped older entries. A candidate nobody has touched
    still deletes as before.
    """
    by_type: dict[str, set[uuid.UUID]] = {kind: set() for kind in _MODELS}
    for change in changes:
        if change.action == CandidateImportChange.CREATED:
            by_type[change.entity_type].add(change.entity_id)

    for kind in (CandidateImportChange.ROLE, CandidateImportChange.CANDIDATE):
        wanted = by_type[kind]
        if not wanted:
            continue
        model = _MODELS[kind]
        rows = (
            (await session.execute(select(model).where(model.id.in_(wanted)))).scalars().all()
        )

        if kind == CandidateImportChange.CANDIDATE and rows:
            work = await _later_human_work(session, import_id, rows)
            kept = []
            for row in rows:
                evidence = work.get(row.id)
                if evidence:
                    outcome.skips.append(
                        UndoSkip(
                            entity_type=kind,
                            entity_id=row.id,
                            field_name="*",
                            reason=(
                                f"candidate {row.id} was kept because {_and(evidence)} since "
                                "this import ran, and deleting the candidate would take that "
                                "work with it"
                            ),
                        )
                    )
                    continue
                kept.append(row)
            rows = kept

        for row in rows:
            await session.delete(row)
            outcome.rows_deleted += 1
        await session.flush()


async def _entities(session, changes: list[CandidateImportChange]) -> dict[uuid.UUID, object]:
    """Every still-existing row an updated change points at, keyed by id."""
    found: dict[uuid.UUID, object] = {}
    for kind, model in _MODELS.items():
        wanted = {
            change.entity_id
            for change in changes
            if change.entity_type == kind and change.action == CandidateImportChange.UPDATED
        }
        if not wanted:
            continue
        rows = (
            (await session.execute(select(model).where(model.id.in_(wanted)))).scalars().all()
        )
        for row in rows:
            found[row.id] = row
    return found


async def undo_import(session, *, tenant_id: uuid.UUID, import_id: uuid.UUID) -> UndoOutcome:
    """Reverse one import as far as it is still safe to, and report the rest.

    Writes on the caller's session and never commits, the same contract
    `apply_import` keeps: the restored values and the import's new state have
    to land together, or an interrupted undo leaves a half-reversed import
    marked `undone`.
    """
    record = await _the_import(session, tenant_id, import_id)
    if record.state not in SETTLED:
        # Anything not settled is still the job's. `parsing` is the obvious
        # case — reversing a run still writing would restore fields it has not
        # finished changing. `pending` is the one that looked harmless and was
        # not: the job can claim it a microsecond later, and an undo that
        # reported success on a file the job then applied in full would be a
        # lie told to the one person relying on undo being true.
        raise ValueError(
            f"import {import_id} is {record.state}; it can only be undone once it has finished"
        )

    # The read above is not the decision — this is. Between the two the job
    # can move the row on, so the state is restated in the WHERE clause and
    # the claim becomes one indivisible statement: the row is checked and
    # marked `undone` together, or no row matches and nothing is reversed.
    # The job's own pickup has the same conditional shape
    # (`import_jobs.run_candidate_import`), so whichever writes first, the
    # other finds its condition false — neither can overwrite the other's
    # answer. The statement also takes the row's write lock for the rest of
    # this transaction, so the reversal below and the state describing it
    # commit together or not at all.
    claimed = await session.execute(
        update(CandidateImport)
        .where(CandidateImport.id == import_id, CandidateImport.state.in_(SETTLED))
        .values(state=CandidateImport.UNDONE)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ValueError(
            f"import {import_id} changed state while it was being undone; nothing was reversed"
        )
    # The core UPDATE went round the identity map, so without this the
    # caller's copy of the row would still read the old state and serialize
    # that into the response.
    await session.refresh(record, ["state"])

    changes = list(
        (
            await session.execute(
                select(CandidateImportChange)
                .where(CandidateImportChange.import_id == import_id)
                .order_by(CandidateImportChange.created_at, CandidateImportChange.id)
            )
        )
        .scalars()
        .all()
    )

    outcome = UndoOutcome()
    await _delete_created(session, import_id, changes, outcome)
    rows = await _entities(session, changes)

    # Newest change first. Should one import ever touch the same field twice,
    # unwinding in reverse leaves the earliest `previous_value` in place; going
    # forwards, the first restore would put back a value the second change's
    # comparison then rejects, and the field would be left mid-way.
    for change in reversed(changes):
        if change.action != CandidateImportChange.UPDATED:
            continue
        row = rows.get(change.entity_id)
        if row is None:
            outcome.skips.append(
                UndoSkip(
                    entity_type=change.entity_type,
                    entity_id=change.entity_id,
                    field_name=change.field_name,
                    reason=(
                        f"the {change.entity_type} row {change.entity_id} no longer exists, "
                        "so there is nothing to restore it on"
                    ),
                )
            )
            continue

        current = _as_text(getattr(row, change.field_name))
        if current != change.new_value:
            outcome.skips.append(
                UndoSkip(
                    entity_type=change.entity_type,
                    entity_id=change.entity_id,
                    field_name=change.field_name,
                    reason=(
                        f"{change.field_name} now reads {current!r}, not the "
                        f"{change.new_value!r} the import wrote, so the later edit was kept"
                    ),
                )
            )
            continue

        # Restoring an identity key can collide with a row that claimed it in
        # the meantime: the import moved A's email from a@x to b@x, somebody
        # then created B with a@x, and putting a@x back on A would trip the
        # unique index and abort the whole reversal. Checked before it is
        # written, the same way `_holder` guards the forward write — a taken
        # key is skipped and reported, never raised into a 500. A NULL
        # `previous_value` restores to NULL, which no index can collide with.
        if (
            change.entity_type == CandidateImportChange.CANDIDATE
            and change.field_name in ("email", "phone_e164")
            and change.previous_value is not None
        ):
            restored_value = _restored(
                _MODELS[change.entity_type], change.field_name, change.previous_value
            )
            # Both columns are strings, so the restored text comes back a
            # string; asserting it narrows the type for `_holder` below.
            assert isinstance(restored_value, str)
            holder = await _holder(session, change.field_name, restored_value, change.entity_id)
            if holder is not None:
                outcome.skips.append(
                    UndoSkip(
                        entity_type=change.entity_type,
                        entity_id=change.entity_id,
                        field_name=change.field_name,
                        reason=(
                            f"{change.field_name} was not restored: {change.previous_value!r} "
                            f"now belongs to candidate {holder}"
                        ),
                    )
                )
                continue

        setattr(
            row,
            change.field_name,
            _restored(_MODELS[change.entity_type], change.field_name, change.previous_value),
        )
        outcome.fields_restored += 1

    outcome.fields_skipped = len(outcome.skips)
    # The row stays rather than being deleted, so the counts on it — and the
    # change log behind them — remain the evidence that the import, and its
    # reversal, both happened. `undone` was written by the claim above, which
    # is what made the rest of this safe to do at all.
    await session.flush()
    return outcome
