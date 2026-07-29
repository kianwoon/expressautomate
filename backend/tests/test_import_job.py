# allow-hardcode: the names, employers and SQL below are test fixture
# content and the import format's fixed header vocabulary, not an oracle.
"""The job that applies an uploaded spreadsheet, and the sweep that finds a
stranded one.

No test here reaches the network: the body store is the in-memory double, and
an import spends no model call at all — it is database work whose size the
uploader chooses, which is the whole reason it has a timeout.
"""

import io
import uuid

import openpyxl
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.candidate import CandidateImport
from app.services.imports.rows import CANDIDATE_SHEET
from app.services.storage.r2 import InMemoryBodyStore, import_key
from app.workers import import_jobs, tasks
from app.workers.import_jobs import run_candidate_import
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import agency  # noqa: F401


@pytest.fixture
def store(monkeypatch) -> InMemoryBodyStore:
    double = InMemoryBodyStore()
    monkeypatch.setattr(import_jobs, "body_store", lambda: double)
    return double


async def _cleanup(tenant_id: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in ("candidate_import_changes", "candidate_imports"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()


async def _seed(
    tenant_id: uuid.UUID,
    store: InMemoryBodyStore,
    content: bytes,
    *,
    kind: str = "csv",
    stem: str = "candidates",
    state: str = CandidateImport.PENDING,
) -> uuid.UUID:
    """An import row and the bytes it names, exactly as the upload leaves them."""
    import_id = uuid.uuid4()
    key = import_key(tenant_id, import_id, stem, kind)
    await store.put_bytes(key, content, "text/csv")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_imports (id, tenant_id, filename, content_type,"
                " byte_size, object_key, state)"
                " VALUES (:i, :t, 'roster.csv', 'text/csv', :b, :k, :s)"
            ),
            {"i": import_id, "t": tenant_id, "b": len(content), "k": key, "s": state},
        )
        await s.commit()
    return import_id


async def _row(import_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT state, candidates_created, roles_created, rows_failed,"
                    " error_report_key FROM candidate_imports WHERE id = :i"
                ),
                {"i": import_id},
            )
        ).one()


def _report(store: InMemoryBodyStore, key: str) -> str:
    # The double keeps (bytes, content_type) beside each key.
    return store.binary_objects[key][0].decode()


def _xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def test_a_csv_of_candidates_is_applied(agency, store):  # noqa: F811
    tenant_id, _user_id = agency
    content = b"full name,email\nJane Tan,jane@acme.sg\n"
    import_id = await _seed(tenant_id, store, content)

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    row = await _row(import_id)
    assert row.state == CandidateImport.DONE
    assert row.candidates_created == 1
    assert row.rows_failed == 0
    # A clean import leaves no report, so the UI offers no link to open.
    assert row.error_report_key is None
    await _cleanup(tenant_id)


async def test_a_csv_is_routed_to_the_sheet_its_key_names(agency, store):  # noqa: F811
    """The only thing that tells a nameless CSV apart: the stem the upload
    route computed. Read as history, these columns become a role."""
    tenant_id, _user_id = agency
    roster = await _seed(tenant_id, store, b"full name,email\nJane Tan,jane@acme.sg\n")
    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(roster))

    history = await _seed(
        tenant_id,
        store,
        b"email,employer,title,start date\njane@acme.sg,Parkway Shenton,Staff Nurse,Mar 2019\n",
        stem="history",
    )
    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(history))

    row = await _row(history)
    assert row.state == CandidateImport.DONE
    assert row.roles_created == 1
    assert row.candidates_created == 0
    await _cleanup(tenant_id)


async def test_a_file_past_the_row_cap_is_refused_naming_the_cap(
    agency,  # noqa: F811
    store,
    monkeypatch,
):
    """"Split the file" is only actionable with a size to split to."""
    monkeypatch.setattr(settings, "IMPORT_MAX_ROWS", 2)
    tenant_id, _user_id = agency
    content = b"full name,email\n" + b"".join(
        f"Person {n},p{n}@acme.sg\n".encode() for n in range(5)
    )
    import_id = await _seed(tenant_id, store, content)

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    row = await _row(import_id)
    assert row.state == CandidateImport.FAILED
    assert row.error_report_key is not None
    assert "2 rows" in _report(store, row.error_report_key)
    # Nothing was half-applied: the cap is hit before any row is looked at.
    assert row.candidates_created == 0
    await _cleanup(tenant_id)


async def test_bad_rows_are_reported_with_their_sheet_and_line(agency, store):  # noqa: F811
    """The run continues past a bad row — a five-hundred-row migration must
    not be lost to one missing email — and the row is named where a recruiter
    can find it in their own file."""
    tenant_id, _user_id = agency
    content = b"full name,email\nJane Tan,jane@acme.sg\nNo Contact,\nBob Lim,bob@acme.sg\n"
    import_id = await _seed(tenant_id, store, content)

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    row = await _row(import_id)
    assert row.state == CandidateImport.DONE
    assert row.candidates_created == 2
    assert row.rows_failed == 1
    # Line 3, not line 2: the header is line 1 and the record's own line
    # travels with it rather than being recomputed from a filtered list.
    assert f"{CANDIDATE_SHEET} line 3:" in _report(store, row.error_report_key)
    await _cleanup(tenant_id)


async def test_a_sheet_nobody_reads_is_reported_rather_than_dropped(agency, store):  # noqa: F811
    tenant_id, _user_id = agency
    content = _xlsx(
        {
            CANDIDATE_SHEET: [["full name", "email"], ["Jane Tan", "jane@acme.sg"]],
            "Sheet1": [["full name", "email"], ["Ghost Person", "ghost@acme.sg"]],
        }
    )
    import_id = await _seed(tenant_id, store, content, kind="xlsx", stem="workbook")

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    row = await _row(import_id)
    assert row.candidates_created == 1
    assert "Sheet1 line 1:" in _report(store, row.error_report_key)
    await _cleanup(tenant_id)


async def test_the_sheet_names_are_matched_case_insensitively(agency, store):  # noqa: F811
    tenant_id, _user_id = agency
    content = _xlsx({"CANDIDATES": [["full name", "email"], ["Jane Tan", "jane@acme.sg"]]})
    import_id = await _seed(tenant_id, store, content, kind="xlsx", stem="workbook")

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    assert (await _row(import_id)).candidates_created == 1
    await _cleanup(tenant_id)


async def test_an_import_already_answered_is_left_alone(agency, store):  # noqa: F811
    """Replaying the job on a terminal row must change nothing — an undone
    import that ran again would silently re-apply everything undo removed."""
    tenant_id, _user_id = agency
    import_id = await _seed(
        tenant_id,
        store,
        b"full name,email\nJane Tan,jane@acme.sg\n",
        state=CandidateImport.UNDONE,
    )

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    row = await _row(import_id)
    assert row.state == CandidateImport.UNDONE
    assert row.candidates_created == 0
    await _cleanup(tenant_id)


async def test_another_tenants_import_is_invisible_to_the_job(agency, store):  # noqa: F811
    """RLS already decided. The job must not fall back to an unscoped read."""
    tenant_id, _user_id = agency
    import_id = await _seed(tenant_id, store, b"full name,email\nJane Tan,jane@acme.sg\n")
    stranger = uuid.uuid4()

    await run_candidate_import(None, tenant_id=str(stranger), import_id=str(import_id))

    assert (await _row(import_id)).state == CandidateImport.PENDING
    await _cleanup(tenant_id)


async def test_a_file_missing_from_storage_fails_with_a_readable_reason(
    agency,  # noqa: F811
    store,
):
    tenant_id, _user_id = agency
    import_id = await _seed(tenant_id, store, b"full name,email\nJane,jane@acme.sg\n")
    store.binary_objects.clear()

    await run_candidate_import(None, tenant_id=str(tenant_id), import_id=str(import_id))

    row = await _row(import_id)
    assert row.state == CandidateImport.FAILED
    assert "upload it again" in _report(store, row.error_report_key)
    await _cleanup(tenant_id)


async def test_the_stuck_sweep_requeues_an_import_stranded_in_parsing(
    agency,  # noqa: F811
    store,
    monkeypatch,
):
    """A worker killed mid-import strands the file forever without this — the
    case a False from enqueue() does not cover, because the process that died
    had already taken the job."""
    tenant_id, _user_id = agency
    import_id = await _seed(
        tenant_id, store, b"full name,email\n", state=CandidateImport.PARSING
    )
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE candidate_imports SET updated_at = now() - interval '2 hours'"
                 " WHERE id = :i"),
            {"i": import_id},
        )
        await s.commit()

    enqueued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        enqueued.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)
    requeued = await tasks.rescan_stuck()

    mine = [
        kwargs
        for name, kwargs in enqueued
        if name == "run_candidate_import" and kwargs["import_id"] == str(import_id)
    ]
    assert mine == [{"tenant_id": str(tenant_id), "import_id": str(import_id)}]
    # Counted alongside the email and document rows rather than separately.
    assert requeued >= 1
    await _cleanup(tenant_id)


def test_the_job_is_registered_with_a_timeout_under_the_name_producers_use():
    """Both halves matter and neither is visible in production until too late.

    An unregistered name is an error inside arq, on the far side of the
    queue, where the producer already saw success; a missing timeout is a
    worker slot held for the life of the process by one oversized file.
    """
    from app.workers.settings import WorkerSettings

    registered = {
        getattr(f, "name", getattr(f, "__name__", None)): f for f in WorkerSettings.functions
    }
    job = registered["run_candidate_import"]
    assert job.timeout_s == settings.IMPORT_JOB_TIMEOUT_SECONDS
