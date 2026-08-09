"""The arq job that applies an uploaded spreadsheet (the import design doc).

Its own module rather than a fifteenth function in `app.workers.jobs`, for
the reason `cv_jobs.py` gives: that file is at the repo's 1500-line ceiling,
and an import shares nothing with mail ingestion but the queue it arrives on.

**The job carries its tenant**, like every other job here. Background work has
no request and therefore no session tenant, and a job naming a mismatched
(tenant, row) pair reads no row under the tenant policy and quietly does
nothing.

**An import is bounded in wall clock.** `settings.py` registers this function
with `IMPORT_JOB_TIMEOUT_SECONDS` as arq's job timeout. A timed-out run leaves
the row at `parsing`, which `rescan_stuck` picks up — the cost of a genuinely
huge file is a retry, not a worker slot held for the life of the process.

**An import is also bounded in attempts.** Every failure this module can name
— a bad file, the row cap, missing bytes — already ends in `failed`, but a
crash inside `apply_import` ends nowhere: the row stays non-terminal and
`rescan_stuck` hands it straight back. `candidate_imports.attempts` counts
pickups, and past `IMPORT_MAX_ATTEMPTS` the run parks the row in `failed`
rather than trying a file that has already defeated it.

**Every failure ends in the error report, not in a column.** `CandidateImport`
has no `error` field on purpose: a five-hundred-row migration can produce
hundreds of problems, so the file in R2 is where a recruiter reads what went
wrong. A run that failed outright writes a one-line report there for exactly
the same reason a run with bad rows writes a long one — one place to look.
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import CandidateImport
from app.services.imports.apply import apply_import
from app.services.imports.rows import (
    CANDIDATE_SHEET,
    HISTORY_SHEET,
    RowProblem,
    parse_candidates,
    parse_roles,
)
from app.services.imports.table import (
    TooManyRows,
    UnreadableTable,
    read_sheets,
    sniff_table,
)
from app.services.storage.r2 import R2BodyStore, import_error_report_key

log = get_logger(__name__)

# The states a run may legitimately start from. `parsing` is included
# deliberately: a worker killed mid-import leaves the row there and
# `rescan_stuck` re-enqueues exactly this job for it, so accepting only
# `pending` would strand that file for ever. `done`, `failed` and `undone`
# are answers — replaying the job on any of them must change nothing.
_RESUMABLE = (CandidateImport.PENDING, CandidateImport.PARSING)

# What the recruiter is told when the bytes we stored cannot be read back.
_MISSING = "The uploaded file could not be read back from storage. Please upload it again."

# What the recruiter is told when the file has defeated every attempt. Phrased
# as a fact about the file rather than as an apology for the worker, because
# the only action left is theirs: nothing will pick this row up again.
_EXHAUSTED = (
    "This file could not be processed after {attempts} attempts, so it was not retried "
    "again. Nothing from it was applied on the attempt that failed. Please check the file "
    "and upload it again."
)


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


def render_report(problems: list[RowProblem]) -> str:
    """The error report, one line per problem, in the recruiter's terms.

    Sheet and line first because that is how the reader navigates: they have
    the file open beside this and want the row number, not our vocabulary.
    """
    return "".join(f"{p.sheet} line {p.line}: {p.reason}\n" for p in problems)


def sheet_for_csv(object_key: str) -> str:
    """Which of the two sheets a CSV upload is standing in for.

    A CSV has no internal sheet name, so the upload route recorded the answer
    in the object key it computed (`import_key`), and this reads it back.
    Going through the key rather than the client's filename matters: the
    filename is attacker-controlled and is kept only for display, whereas the
    key's stem is one of our own two words, chosen by the route from a
    validated form field.
    """
    stem = object_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return HISTORY_SHEET if stem.lower() == HISTORY_SHEET.lower() else CANDIDATE_SHEET


async def _fail(tenant: uuid.UUID, import_id: uuid.UUID, reason: str) -> None:
    """Park the import in `failed` with a report saying why.

    The report is written before the state moves so that a `failed` row a
    recruiter is looking at always has something to open. Written even for a
    whole-file failure, because the panel offers exactly one link and it must
    not sometimes lead nowhere.
    """
    key = import_error_report_key(tenant, import_id)
    await body_store().put_bytes(key, reason.encode(), "text/plain; charset=utf-8")
    async with tenant_session(tenant) as session:
        row = await session.get(CandidateImport, import_id)
        if row is None:  # pragma: no cover - deleted mid-run
            return
        row.state = CandidateImport.FAILED
        row.error_report_key = key
        await session.commit()


def _split(
    sheets: dict[str, list[dict[str, str]]], width_problems: list
) -> tuple[list, list, list[RowProblem]]:
    """Route each sheet in the workbook to the parser that understands it.

    Matched case-insensitively because `Candidates`, `candidates` and
    `CANDIDATES` are the same intent from three people, and a workbook
    exported from an old system rarely capitalises the way ours does. A sheet
    named anything else is reported rather than silently dropped: a recruiter
    whose data sat on a tab called `Sheet1` deserves to be told that, not to
    see an import that succeeded and did nothing.

    The `width_problems` `read_sheets` skipped arrive here as the same kind
    of report: `RowWidthProblem` and `RowProblem` are the same three fields
    (sheet, line, reason), so each becomes a line in the error report — a
    misaligned CSV row is a row problem, exactly like a row the parser
    refused, and the run continues past it.
    """
    candidates: list = []
    roles: list = []
    problems: list[RowProblem] = [
        RowProblem(sheet=p.sheet, line=p.line, reason=p.reason) for p in width_problems
    ]

    for name, rows in sheets.items():
        lowered = name.strip().lower()
        if lowered == CANDIDATE_SHEET.lower():
            parsed, found = parse_candidates(rows, sheet=name)
            candidates.extend(parsed)
            problems.extend(found)
        elif lowered == HISTORY_SHEET.lower():
            parsed, found = parse_roles(rows, sheet=name)
            roles.extend(parsed)
            problems.extend(found)
        elif rows:
            # Line 1 is the header — the only line that can be blamed for a
            # sheet nobody reads, since the objection is to its name.
            problems.append(
                RowProblem(
                    sheet=name,
                    line=1,
                    reason=(
                        f"this sheet was ignored: only sheets named "
                        f"{CANDIDATE_SHEET!r} and {HISTORY_SHEET!r} are read"
                    ),
                )
            )

    return candidates, roles, problems


async def run_candidate_import(ctx, *, tenant_id: str, import_id: str) -> None:
    """Read one uploaded spreadsheet and apply what it said.

    Failure discipline mirrors `parse_candidate_cv`. The row moves to
    `parsing` before the long operation, because arq only reschedules on
    `Retry` and nothing here raises one: an infrastructure failure is a
    permanently failed job and `rescan_stuck` re-enqueues the row once the
    outage ends. Leaving it at `pending` across the run would instead let the
    sweep apply the same file twice, concurrently, while the first attempt
    was still writing.
    """
    tenant = uuid.UUID(tenant_id)
    record = uuid.UUID(import_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                select(CandidateImport).where(CandidateImport.id == record)
            )
        ).scalar_one_or_none()
        if row is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info("import_skipped_unknown_row", candidate_import_id=import_id)
            return
        if row.state not in _RESUMABLE:
            log.info(
                "import_skipped_already_answered",
                candidate_import_id=import_id,
                state=row.state,
            )
            return

        # The claim is a conditional UPDATE, not the read above followed by a
        # write. The read is only good enough to log with: between it and the
        # write, an undo can move this row to `undone`, and a blind write
        # would put `parsing` straight over that decision and then apply the
        # whole file the recruiter was told had been reversed. Restating the
        # state in the WHERE clause makes the check and the write one
        # indivisible statement — whoever loses simply matches no row.
        #
        # The attempt is spent in that same statement. Counting at the end
        # instead would count nothing on exactly the runs this bounds — a
        # crash inside `apply_import` never reaches an end — and a file that
        # deterministically crashes would be re-enqueued by `rescan_stuck`
        # for ever, one worker slot per sweep, silently.
        claimed = (
            await session.execute(
                update(CandidateImport)
                .where(
                    CandidateImport.id == record,
                    CandidateImport.state.in_(_RESUMABLE),
                )
                .values(
                    state=CandidateImport.PARSING,
                    attempts=CandidateImport.attempts + 1,
                )
                .returning(CandidateImport.object_key, CandidateImport.attempts)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            log.info("import_skipped_claimed_elsewhere", candidate_import_id=import_id)
            return
        object_key, attempts = claimed
        await session.commit()

    if attempts > settings.IMPORT_MAX_ATTEMPTS:
        # Terminal, so `rescan_stuck` stops seeing it. The row is claimed
        # first and refused second on purpose: leaving it at `pending` while
        # refusing would let the sweep pick it up again on the next pass and
        # discover the same thing, which is the loop rather than the end of it.
        log.warning(
            "import_attempts_exhausted", candidate_import_id=import_id, attempts=attempts
        )
        await _fail(tenant, record, _EXHAUSTED.format(attempts=settings.IMPORT_MAX_ATTEMPTS))
        return

    data = await body_store().get_bytes(object_key)
    if not data:
        await _fail(tenant, record, _MISSING)
        return

    # Sniffed again rather than trusted from the row. The upload already
    # refused anything else, but this job also runs from `rescan_stuck`,
    # against bytes that have been sitting in object storage — and the cost
    # of asking is one function call over bytes already in memory.
    kind = sniff_table(data)
    if kind is None:
        await _fail(tenant, record, "This file is no longer a readable CSV or Excel spreadsheet.")
        return

    try:
        sheets, width_problems = read_sheets(
            data,
            kind,
            budget=settings.IMPORT_INFLATE_BUDGET_BYTES,
            max_rows=settings.IMPORT_MAX_ROWS,
            sheet_name=sheet_for_csv(object_key) if kind == "csv" else None,
        )
    except TooManyRows:
        # Named rather than passed through, because the caller's number is
        # the actionable half: "split the file" needs a size to split to.
        await _fail(
            tenant,
            record,
            f"This file has more than the {settings.IMPORT_MAX_ROWS} rows one import may "
            "carry on a single sheet. Please split it and upload the parts.",
        )
        return
    except UnreadableTable as exc:
        await _fail(tenant, record, f"This file could not be read as a spreadsheet: {exc}")
        return

    candidates, roles, problems = _split(sheets, width_problems)

    async with tenant_session(tenant) as session:
        outcome = await apply_import(
            session,
            tenant_id=tenant,
            import_id=record,
            candidates=candidates,
            roles=roles,
            today=_today(),
        )
        problems = problems + outcome.problems

        key = None
        if problems:
            key = import_error_report_key(tenant, record)

        row = await session.get(CandidateImport, record)
        if row is None:  # pragma: no cover - deleted mid-run
            return
        row.candidates_created = outcome.candidates_created
        row.candidates_updated = outcome.candidates_updated
        row.roles_created = outcome.roles_created
        row.roles_updated = outcome.roles_updated
        row.held_by_colleagues = outcome.held_by_colleagues
        row.rows_failed = len(problems)
        row.error_report_key = key
        row.state = CandidateImport.DONE
        # The report is written before the commit that publishes its key, so
        # a crash between the two leaves an orphan object rather than a row
        # pointing at a file that does not exist.
        if key is not None:
            await body_store().put_bytes(
                key, render_report(problems).encode(), "text/plain; charset=utf-8"
            )
        await session.commit()

    log.info(
        "import_applied",
        candidate_import_id=import_id,
        candidates_created=outcome.candidates_created,
        candidates_updated=outcome.candidates_updated,
        roles_created=outcome.roles_created,
        roles_updated=outcome.roles_updated,
        held_by_colleagues=outcome.held_by_colleagues,
        rows_failed=len(problems),
    )


def _today() -> date:
    """Today in UTC, passed in rather than read inside `apply_import`.

    An end date in the future is a data error the import reports, and which
    day counts as "future" is a decision the caller owns — the same reason
    `apply_import` takes the date as an argument at all.
    """
    return datetime.now(UTC).date()
