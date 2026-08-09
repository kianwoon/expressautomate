"""Tests for `app.services.imports.table` — bytes to rows, nothing more."""

import csv
import io
import zipfile

import openpyxl
import pytest

from app.services.imports.table import (
    TooManyRows,
    UnreadableTable,
    read_sheets,
    sniff_table,
)


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(name)
        for row in rows:
            worksheet.append(row)
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def test_csv_header_and_two_rows_yield_two_dicts():
    data = b"Name,Email\nAlice,alice@example.com\nBob,bob@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    rows = sheets["csv"]
    assert rows == [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
    ]


def test_csv_headers_case_and_space_insensitive():
    data = b" Name , EMAIL \nAlice,alice@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name": "Alice", "email": "alice@example.com"}]


def test_bare_zip_without_workbook_xml_is_not_xlsx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a workbook")
    assert sniff_table(buf.getvalue()) is None


def test_real_xlsx_is_recognised_and_read():
    data = _xlsx_bytes(
        {
            "Candidates": [["Name", "Email"], ["Alice", "alice@example.com"]],
            "History": [["Company", "Role"], ["Acme", "Recruiter"]],
        }
    )
    assert sniff_table(data) == "xlsx"

    sheets, _ = read_sheets(data, "xlsx", budget=10_000_000, max_rows=100)
    assert sheets["Candidates"] == [{"name": "Alice", "email": "alice@example.com"}]
    assert sheets["History"] == [{"company": "Acme", "role": "Recruiter"}]


def test_neither_csv_nor_xlsx_returns_none():
    assert sniff_table(b"\x00\x01\x02\x03binary garbage\xff\xfe") is None


def test_too_many_rows_names_the_cap():
    data = b"Name\n" + b"".join(f"Row{i}\n".encode() for i in range(5))
    with pytest.raises(TooManyRows, match="3"):
        read_sheets(data, "csv", budget=10_000, max_rows=3)


def test_whitespace_only_cell_reads_as_empty():
    data = b"Name,Note\nAlice,   \n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name": "Alice", "note": ""}]


def test_merged_cell_reads_as_empty_in_later_rows():
    data = _xlsx_bytes({"Sheet1": [["Name", "Team"], ["Alice", "Eng"], ["Bob", None]]})
    sheets, _ = read_sheets(data, "xlsx", budget=10_000_000, max_rows=100)
    assert sheets["Sheet1"] == [
        {"name": "Alice", "team": "Eng"},
        {"name": "Bob", "team": ""},
    ]


def test_corrupt_xlsx_raises_unreadable_table():
    with pytest.raises(UnreadableTable):
        read_sheets(b"PK garbage that is not a real zip", "xlsx", budget=10_000, max_rows=100)


def test_csv_too_many_rows_raises_before_materialising_whole_file():
    """The cap must fire while streaming, never after the whole file is read.

    A csv row is much bigger than 1 char, so `csv.reader` itself does no
    look-ahead worth mentioning — the thing we're actually guarding against
    is `read_sheets` collecting the reader into a `list(...)` before
    `max_rows` is ever checked. `_ExplodingLines` stands in for the
    underlying line source and raises if pulled past the point the cap
    should have already fired at (header + `max_rows` + 1 data rows) —
    against the pre-fix code, which built `records = list(reader)` first,
    this raises `AssertionError` instead of `TooManyRows` because far more
    lines than that get pulled before the cap is ever consulted.
    """

    class _ExplodingLines:
        def __init__(self, limit: int):
            self._limit = limit
            self._n = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self._n > self._limit:
                raise AssertionError(
                    "csv source was pulled past the point TooManyRows should "
                    "have already fired — the cap is not being enforced inline"
                )
            self._n += 1
            return f"Row{self._n}\n"

    max_rows = 3
    # header (1) + rows up to and including the one that trips the cap
    # (max_rows + 1) — one line of slack for csv.reader's own buffering.
    limit = 1 + max_rows + 1 + 1

    import unittest.mock as mock

    real_reader = csv.reader

    def _fake_reader(_source, *args, **kwargs):
        # `read_sheets("csv", ...)` builds its own `io.StringIO(text)` from
        # the decoded bytes and hands that to `csv.reader`; we swap in
        # `_ExplodingLines` here instead so the exception proves how far
        # into the source `_read_csv` actually reached, regardless of what
        # object it passed in.
        return real_reader(_ExplodingLines(limit), *args, **kwargs)

    data = b"Name\n" + b"".join(f"Row{i}\n".encode() for i in range(1_000))
    with mock.patch("app.services.imports.table.csv.reader", side_effect=_fake_reader):
        with pytest.raises(TooManyRows, match=str(max_rows)):
            read_sheets(data, "csv", budget=10_000, max_rows=max_rows)


def test_duplicate_normalised_headers_raise_unreadable_table():
    data = b"Email,email \nalice@example.com,dup\n"
    with pytest.raises(UnreadableTable, match="email"):
        read_sheets(data, "csv", budget=10_000, max_rows=100)


def test_xlsx_duplicate_headers_raise_unreadable_table():
    data = _xlsx_bytes({"Sheet1": [["Email", "email "], ["a@x.com", "b@x.com"]]})
    with pytest.raises(UnreadableTable, match="email"):
        read_sheets(data, "xlsx", budget=10_000_000, max_rows=100)


def test_csv_sheet_name_defaults_to_csv_but_can_be_overridden():
    data = b"Name,Email\nAlice,alice@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100, sheet_name="History")
    assert list(sheets.keys()) == ["History"]
    assert sheets["History"] == [{"name": "Alice", "email": "alice@example.com"}]


# A row wider than the header ------------------------------------------------


def test_csv_row_wider_than_header_is_reported_and_skipped():
    """An unquoted comma shifts the columns; the extra cell is its trace. The
    row is skipped and reported as one row problem — never silently dropped,
    and never a reason to refuse the whole file."""
    data = (
        b"Name,Email\n"
        b"Alice,alice@example.com,stray,stray\n"
        b"Bob,bob@example.com\n"
    )
    sheets, problems = read_sheets(data, "csv", budget=10_000, max_rows=100)

    # The good row still lands; the misaligned one costs only itself.
    assert sheets["csv"] == [{"name": "Bob", "email": "bob@example.com"}]
    assert len(problems) == 1
    assert problems[0].sheet == "csv"
    assert problems[0].line == 2
    assert "4 columns" in problems[0].reason
    assert "header has 2" in problems[0].reason


def test_misaligned_csv_rows_keep_their_true_lines():
    """Two bad rows report their own line numbers, and following good rows
    still read as themselves."""
    data = (
        b"Name,Email\n"
        b"Alice,alice@example.com,stray\n"
        b"Carol,carol@example.com\n"
        b"Dave,dave@example.com,stray\n"
        b"Erin,erin@example.com\n"
    )
    sheets, problems = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [
        {"name": "Carol", "email": "carol@example.com"},
        {"name": "Erin", "email": "erin@example.com"},
    ]
    assert [p.line for p in problems] == [2, 4]


def test_csv_row_shorter_than_header_still_reads():
    """Trailing empty cells are the ordinary CSV case and must not be refused —
    the missing column simply reads empty, which the row parsers report on."""
    data = b"Name,Email,Phone\nAlice,alice@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    # The absent phone cell is a missing key, not a "": `parse_candidates`
    # reads `row.get("phone") or ""`, so both are the same downstream.
    assert sheets["csv"] == [{"name": "Alice", "email": "alice@example.com"}]


def test_xlsx_row_wider_than_header_is_not_refused():
    """XLSX rows are uniformly padded and ragged widths there are an openpyxl
    quirk, not a corruption signal — the strict width rule is CSV-only."""
    data = _xlsx_bytes({"Sheet1": [["Name", "Email"], ["Alice", "a@x.com", "extra"]]})
    sheets, _ = read_sheets(data, "xlsx", budget=10_000_000, max_rows=100)
    assert sheets["Sheet1"] == [{"name": "Alice", "email": "a@x.com"}]


# Encodings and delimiters ---------------------------------------------------


def test_utf16_csv_with_bom_is_sniffed_and_read():
    """Excel's 'Unicode Text' export is UTF-16LE with a BOM; the utf-16 codec
    honours and strips the mark, so the file reads exactly like a UTF-8 one."""
    data = "Name,Email\nAlice,alice@example.com\n".encode("utf-16")
    assert sniff_table(data) == "csv"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name": "Alice", "email": "alice@example.com"}]


def test_utf16_without_bom_falls_back_to_utf8_not_a_crash():
    """No BOM means the endianness is unknowable, and the file cannot be
    told apart from UTF-8 text containing NULs — so it is not refused, it is
    read as UTF-8 and the NUL-stuffed cells fail per-row parsing downstream.
    Pinned here so nobody mistakes that for a silent success."""
    data = "Name,Email\nAlice,alice@example.com\n".encode("utf-16-le")
    assert sniff_table(data) == "csv"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"]  # parses; the cells are NUL-stuffed garbage


def test_semicolon_delimited_csv_is_read():
    """An Excel-locale CSV uses `;`; the delimiter is taken from the header
    line, where quotes are almost never in play."""
    data = b"Name;Email\nAlice;alice@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name": "Alice", "email": "alice@example.com"}]


def test_tab_delimited_csv_is_read():
    data = b"Name\tEmail\nAlice\talice@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name": "Alice", "email": "alice@example.com"}]


def test_comma_wins_the_delimiter_tie():
    """A header with one comma and one semicolon is a comma file, not a
    semicolon file — comma is the conventional default on a tie, so the
    semicolon stays inside the first column name."""
    data = b"Name;Full,Email\nAlice;A,alice@example.com\n"
    sheets, _ = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name;full": "Alice;A", "email": "alice@example.com"}]
