"""Turn a table row into a typed record, or into a sentence a recruiter can act on.

Pure: no database, no network, no settings. `app/services/imports/table.py`
already turned the uploaded file into `dict[sheet_name, list[dict[str, str]]]`,
each row keyed by a lower-cased, stripped header. This module takes those row
dicts and answers "is this a candidate/role, or is it a problem?" — matching
those candidates against the database, writing them, and undoing the write are
later tasks.

A parse never raises. Every failure a row could have becomes a `RowProblem`
naming the sheet and the recruiter's own line number, and the run continues —
one bad cell in row 400 of a five-hundred-row migration must not cost the
other 499.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.services.candidate_naming import normalize_email, normalize_phone

# Fixed, case-insensitive headers. Positional guessing is exactly the failure
# mode undo exists to recover from (see the import design doc) — a column is
# read by name or not at all.
_FULL_NAME = "full name"
_EMAIL = "email"
_PHONE = "phone"
_TITLE = "title"
_EMPLOYER = "employer"
_LOCATION = "location"
_STARTED = "start date"
_ENDED = "end date"
_DESCRIPTION = "description"


@dataclass(frozen=True)
class RowProblem:
    """One row that could not become a record, in a recruiter's own words."""

    sheet: str
    line: int
    reason: str


@dataclass(frozen=True)
class CandidateRecord:
    full_name: str
    email: str | None
    phone_raw: str | None
    phone_e164: str | None
    current_title: str | None
    current_employer: str | None
    location: str | None


@dataclass(frozen=True)
class RoleRecord:
    # Not a candidate_id — history rows are matched to a candidate by email
    # or phone, the same identity resolution `find_candidate` already does,
    # so a role carries the keys to look that candidate up rather than a
    # foreign key it has no way to know yet.
    candidate_email: str | None
    candidate_phone: str | None
    employer: str
    title: str
    started_on: date | None
    started_precision: str | None
    ended_on: date | None
    ended_precision: str | None
    location: str | None
    description: str | None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_WORD = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

# openpyxl's `data_only=True` hands `table.py` real `datetime`/`date` objects
# for a genuine date cell, and `_normalise_cell` there stringifies everything
# before we ever see it — so a day-precision cell arrives here already
# rendered as `str(datetime(...))`, e.g. "2019-03-14 00:00:00". Matching that
# form first, before the looser patterns below, is what makes a real date
# cell distinguishable from a typed "2019-03-14".
_DATETIME_STR = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T]\d{2}:\d{2}:\d{2}$")
_ISO_DAY = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MONTH_YEAR = re.compile(rf"^{_MONTH_WORD}\.?\s+(\d{{4}})$", re.IGNORECASE)
_YEAR_ONLY = re.compile(r"^(\d{4})$")


def _parse_cell_date(raw: str) -> tuple[date | None, str | None]:
    """The date a cell actually states, at the precision it actually states it.

    Never invents a missing component (§15) — "Mar 2019" becomes month
    precision with no day, never the 1st of March pretending to be exact.
    An ambiguous all-numeric date (`3/4/2019`, where both leading numbers
    could be the month) is not guessed at either; it falls back to year
    precision, the same call `app/services/cv/persist.py:_read_parts` makes
    for the identical shape in free CV text, for the same reason: guessing
    day-first would store a fact the cell never stated.
    """
    text = raw.strip()
    if not text:
        return None, None

    match = _DATETIME_STR.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return date(year, month, day), "day"
        except ValueError:
            return None, None

    match = _ISO_DAY.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return date(year, month, day), "day"
        except ValueError:
            return None, None

    match = _ISO_MONTH.match(text)
    if match:
        year, month = (int(g) for g in match.groups())
        try:
            return date(year, month, 1), "month"
        except ValueError:
            return None, None

    match = _SLASH.match(text)
    if match:
        a, b, year = (int(g) for g in match.groups())
        # Both fields <= 12: genuinely ambiguous, which is the day and which
        # is the month. Only the year survives.
        if a <= 12 and b <= 12:
            return date(year, 1, 1), "year"
        # Day-first, unconditionally — mirrors `app/services/cv/persist.py:
        # _read_parts` exactly, on purpose: the two import paths must classify
        # the same cell the same way. The old rule here picked whichever
        # field exceeded 12 as the day, so `4/25/2019` was read as 25 April —
        # a date the CV path refuses to assert (it stays day-first, gets an
        # invalid month, and drops the date). Reinterpreting the order just
        # because the day-first reading fails is itself a fabrication (§15):
        # it substitutes "some order that produces a valid date" for "the
        # order the cell actually wrote".
        try:
            return date(year, b, a), "day"
        except ValueError:
            # Day-first gave an invalid month (`4/25/2019`: month 25). No
            # fallback to month precision or year precision here — `_read_parts`
            # doesn't offer one either; `stored_date`'s except clause just
            # drops the date and keeps the role, which this matches.
            return None, None

    match = _MONTH_YEAR.match(text)
    if match:
        month = _MONTHS[match.group(1)[:3].lower()]
        year = int(match.group(2))
        return date(year, month, 1), "month"

    match = _YEAR_ONLY.match(text)
    if match:
        return date(int(match.group(1)), 1, 1), "year"

    return None, None


def _is_blank_row(row: dict[str, str]) -> bool:
    return not any(value.strip() for value in row.values() if value)


def parse_candidates(
    rows: list[dict[str, str]], *, sheet: str
) -> tuple[list[CandidateRecord], list[RowProblem]]:
    records: list[CandidateRecord] = []
    problems: list[RowProblem] = []

    for index, row in enumerate(rows):
        # `index` is 0-based over data rows; the header itself is line 1 in
        # the recruiter's spreadsheet, so the first data row is line 2.
        line = index + 2
        if _is_blank_row(row):
            continue

        full_name = (row.get(_FULL_NAME) or "").strip()
        raw_email = row.get(_EMAIL) or ""
        raw_phone = row.get(_PHONE) or ""
        email = normalize_email(raw_email) if raw_email.strip() else None
        phone_e164 = normalize_phone(raw_phone) if raw_phone.strip() else None

        if raw_phone.strip() and phone_e164 is None:
            problems.append(
                RowProblem(sheet=sheet, line=line, reason=f"phone {raw_phone!r} could not be read")
            )
            continue

        if not email and not phone_e164:
            problems.append(
                RowProblem(
                    sheet=sheet, line=line, reason="no email or phone to match this person by"
                )
            )
            continue

        records.append(
            CandidateRecord(
                full_name=full_name,
                email=email,
                phone_raw=raw_phone.strip() or None,
                phone_e164=phone_e164,
                current_title=(row.get(_TITLE) or "").strip() or None,
                current_employer=(row.get(_EMPLOYER) or "").strip() or None,
                location=(row.get(_LOCATION) or "").strip() or None,
            )
        )

    return records, problems


def parse_roles(
    rows: list[dict[str, str]], *, sheet: str
) -> tuple[list[RoleRecord], list[RowProblem]]:
    records: list[RoleRecord] = []
    problems: list[RowProblem] = []

    for index, row in enumerate(rows):
        line = index + 2
        if _is_blank_row(row):
            continue

        raw_email = row.get(_EMAIL) or ""
        raw_phone = row.get(_PHONE) or ""
        email = normalize_email(raw_email) if raw_email.strip() else None
        phone_e164 = normalize_phone(raw_phone) if raw_phone.strip() else None

        if raw_phone.strip() and phone_e164 is None:
            problems.append(
                RowProblem(sheet=sheet, line=line, reason=f"phone {raw_phone!r} could not be read")
            )
            continue

        if not email and not phone_e164:
            problems.append(
                RowProblem(
                    sheet=sheet,
                    line=line,
                    reason="no email or phone to match this row to a candidate",
                )
            )
            continue

        employer = (row.get(_EMPLOYER) or "").strip()
        title = (row.get(_TITLE) or "").strip()
        if not employer and not title:
            problems.append(
                RowProblem(sheet=sheet, line=line, reason="no employer or title for this role")
            )
            continue

        started_on, started_precision = _parse_cell_date(row.get(_STARTED) or "")
        ended_on, ended_precision = _parse_cell_date(row.get(_ENDED) or "")
        # `None, None` here means the cell could not be honestly read (an
        # unparseable string, or a day-first reading with an invalid month —
        # see `_parse_cell_date`'s `_SLASH` branch). That drops only the date;
        # the role itself is still recorded with whichever precision came
        # back. `app/services/cv/persist.py:stored_date` makes the same call
        # for the same reason — a half-read date is not a reason to discard a
        # role a recruiter did type or a CV did name.

        records.append(
            RoleRecord(
                candidate_email=email,
                candidate_phone=phone_e164,
                employer=employer,
                title=title,
                started_on=started_on,
                started_precision=started_precision,
                ended_on=ended_on,
                ended_precision=ended_precision,
                location=(row.get(_LOCATION) or "").strip() or None,
                description=(row.get(_DESCRIPTION) or "").strip() or None,
            )
        )

    return records, problems
