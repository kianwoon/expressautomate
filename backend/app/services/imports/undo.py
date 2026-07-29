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
informational only.

Whatever undo would not do is reported rather than swallowed. An undo that
reversed less than the whole import must say so — a caller that presented a
partial reversal as a clean one would be telling the recruiter their data is
back when some of it is not.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect, select

from app.models.candidate import (
    Candidate,
    CandidateImport,
    CandidateImportChange,
    CandidateRole,
)

# Deliberately imported rather than re-implemented. `_as_text` is how the
# change log rendered every value on the way in, so it is the only rendering
# that can be compared against `new_value` on the way back. Two independent
# copies that drifted apart would make undo silently inert — every comparison
# failing, every field "skipped", and nothing about the result looking wrong.
from app.services.imports.apply import _as_text

# The mapped class behind each `entity_type`. Undo reads the column types off
# these to put a text value back as whatever the column actually holds.
_MODELS = {
    CandidateImportChange.CANDIDATE: Candidate,
    CandidateImportChange.ROLE: CandidateRole,
}

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


async def _delete_created(
    session, changes: list[CandidateImportChange], outcome: UndoOutcome
) -> None:
    """Remove every row this import brought into existence.

    Roles go first, and the order is the difference between an honest count
    and a misleading one. Deleting the candidate first takes its roles with it
    on `fk_candidate_roles_candidate_same_tenant`'s cascade, and the roles are
    then no longer there to be found — so a run that removed a person and
    their job would report one row deleted, understating what it did to the
    recruiter reading the confirmation. Deleting a role never removes a
    candidate, so this direction has no such shadow.
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
    if record.state == CandidateImport.PARSING:
        # The run is still writing. Reversing it now would race it, and undo
        # would restore fields the import has not finished changing yet.
        raise ValueError(
            f"import {import_id} is still parsing; it cannot be undone until it finishes"
        )

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
    await _delete_created(session, changes, outcome)
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

        setattr(
            row,
            change.field_name,
            _restored(_MODELS[change.entity_type], change.field_name, change.previous_value),
        )
        outcome.fields_restored += 1

    outcome.fields_skipped = len(outcome.skips)
    # The row stays rather than being deleted, so the counts on it — and the
    # change log behind them — remain the evidence that the import, and its
    # reversal, both happened.
    record.state = CandidateImport.UNDONE
    await session.flush()
    return outcome
