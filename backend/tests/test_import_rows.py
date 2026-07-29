"""Tests for `app.services.imports.rows` — rows to typed records, or problems."""

from datetime import date

from app.services.imports.rows import (
    CandidateRecord,
    RoleRecord,
    RowProblem,
    parse_candidates,
    parse_roles,
)


def test_candidate_row_with_email_and_phone_parses():
    rows = [{"full name": "Alice Tan", "email": "Alice@Example.com", "phone": "91234567"}]
    records, problems = parse_candidates(rows, sheet="Candidates")
    assert problems == []
    assert records == [
        CandidateRecord(
            full_name="Alice Tan",
            email="alice@example.com",
            phone_raw="91234567",
            phone_e164="+6591234567",
            current_title=None,
            current_employer=None,
            location=None,
        )
    ]


def test_candidate_row_with_neither_email_nor_phone_is_a_problem():
    rows = [{"full name": "No Contact"}]
    records, problems = parse_candidates(rows, sheet="Candidates")
    assert records == []
    assert problems == [
        RowProblem(
            sheet="Candidates", line=2, reason="no email or phone to match this person by"
        )
    ]


def test_unnormalisable_phone_is_a_problem_with_readable_reason():
    rows = [{"full name": "Bad Phone", "phone": "12a34"}]
    records, problems = parse_candidates(rows, sheet="Candidates")
    assert records == []
    assert len(problems) == 1
    assert problems[0].sheet == "Candidates"
    assert problems[0].line == 2
    assert "12a34" in problems[0].reason


def test_entirely_empty_row_is_skipped_silently():
    rows = [{"full name": "", "email": "", "phone": "  "}]
    records, problems = parse_candidates(rows, sheet="Candidates")
    assert records == []
    assert problems == []


def test_line_number_counts_the_header():
    rows = [
        {"full name": "First", "email": "first@example.com"},
        {"full name": "", "email": ""},  # blank, skipped, does not shift numbering
        {"full name": "Third"},  # no email/phone -> problem at line 4
    ]
    records, problems = parse_candidates(rows, sheet="Candidates")
    assert len(records) == 1
    assert problems == [
        RowProblem(
            sheet="Candidates", line=4, reason="no email or phone to match this person by"
        )
    ]


def test_role_row_missing_employer_and_title_is_a_problem():
    rows = [{"email": "a@example.com", "start date": "2019"}]
    records, problems = parse_roles(rows, sheet="History")
    assert records == []
    assert problems == [
        RowProblem(sheet="History", line=2, reason="no employer or title for this role")
    ]


def test_role_row_with_neither_email_nor_phone_is_a_problem():
    rows = [{"employer": "Acme", "title": "Recruiter"}]
    records, problems = parse_roles(rows, sheet="History")
    assert records == []
    assert problems == [
        RowProblem(
            sheet="History", line=2, reason="no email or phone to match this row to a candidate"
        )
    ]


def test_year_only_date_yields_year_precision():
    rows = [
        {"email": "a@example.com", "employer": "Acme", "title": "Recruiter", "start date": "2019"}
    ]
    records, _ = parse_roles(rows, sheet="History")
    assert records[0].started_on == date(2019, 1, 1)
    assert records[0].started_precision == "year"


def test_month_year_cell_yields_month_precision_and_no_day():
    rows = [
        {
            "email": "a@example.com",
            "employer": "Acme",
            "title": "Recruiter",
            "start date": "Mar 2019",
        }
    ]
    records, _ = parse_roles(rows, sheet="History")
    role = records[0]
    assert role.started_precision == "month"
    assert role.started_on == date(2019, 3, 1)
    # No day was invented: the stored date lands on the 1st only because a
    # date column needs a full date, and the precision travelling with it is
    # what stops a caller from rendering "1 March 2019" as fact.
    assert role.started_on.day == 1


def test_iso_month_cell_also_yields_month_precision():
    rows = [
        {
            "email": "a@example.com",
            "employer": "Acme",
            "title": "Recruiter",
            "start date": "2019-03",
        }
    ]
    records, _ = parse_roles(rows, sheet="History")
    assert records[0].started_precision == "month"
    assert records[0].started_on == date(2019, 3, 1)


def test_real_date_cell_yields_day_precision():
    # `table.py` stringifies a genuine openpyxl date cell via str(datetime),
    # e.g. "2019-03-14 00:00:00" — this is what reaches us for a real date.
    rows = [
        {
            "email": "a@example.com",
            "employer": "Acme",
            "title": "Recruiter",
            "start date": "2019-03-14 00:00:00",
        }
    ]
    records, _ = parse_roles(rows, sheet="History")
    assert records[0].started_precision == "day"
    assert records[0].started_on == date(2019, 3, 14)


def test_iso_date_string_also_yields_day_precision():
    rows = [
        {
            "email": "a@example.com",
            "employer": "Acme",
            "title": "Recruiter",
            "start date": "2019-03-14",
        }
    ]
    records, _ = parse_roles(rows, sheet="History")
    assert records[0].started_precision == "day"
    assert records[0].started_on == date(2019, 3, 14)


def test_ambiguous_slash_date_falls_back_to_year_precision():
    rows = [
        {
            "email": "a@example.com",
            "employer": "Acme",
            "title": "Recruiter",
            "start date": "3/4/2019",
        }
    ]
    records, _ = parse_roles(rows, sheet="History")
    role = records[0]
    assert role.started_precision == "year"
    assert role.started_on == date(2019, 1, 1)
    assert role.started_on.month == 1
    assert role.started_on.day == 1


def test_unambiguous_slash_date_yields_day_precision():
    rows = [
        {
            "email": "a@example.com",
            "employer": "Acme",
            "title": "Recruiter",
            "start date": "25/4/2019",
        }
    ]
    records, _ = parse_roles(rows, sheet="History")
    role = records[0]
    assert role.started_precision == "day"
    assert role.started_on == date(2019, 4, 25)


def test_role_entirely_empty_row_is_skipped_silently():
    rows = [{"email": "", "employer": "", "title": "", "start date": ""}]
    records, problems = parse_roles(rows, sheet="History")
    assert records == []
    assert problems == []


def test_role_unnormalisable_phone_is_a_problem():
    rows = [{"phone": "notaphone", "employer": "Acme", "title": "Recruiter"}]
    records, problems = parse_roles(rows, sheet="History")
    assert records == []
    assert len(problems) == 1
    assert "notaphone" in problems[0].reason


def test_role_record_has_matching_keys_not_a_candidate_id():
    rows = [
        {"phone": "91234567", "employer": "Acme", "title": "Recruiter"},
    ]
    records, problems = parse_roles(rows, sheet="History")
    assert problems == []
    role = records[0]
    assert isinstance(role, RoleRecord)
    assert role.candidate_phone == "+6591234567"
    assert role.candidate_email is None


def test_parse_never_raises_on_garbage_row():
    rows = [{"full name": None, "email": None, "phone": None}]  # type: ignore[dict-item]
    records, problems = parse_candidates(rows, sheet="Candidates")
    assert records == []
    assert problems == []
