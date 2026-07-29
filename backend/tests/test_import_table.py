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
    sheets = read_sheets(data, "csv", budget=10_000, max_rows=100)
    rows = sheets["csv"]
    assert rows == [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
    ]


def test_csv_headers_case_and_space_insensitive():
    data = b" Name , EMAIL \nAlice,alice@example.com\n"
    sheets = read_sheets(data, "csv", budget=10_000, max_rows=100)
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

    sheets = read_sheets(data, "xlsx", budget=10_000_000, max_rows=100)
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
    sheets = read_sheets(data, "csv", budget=10_000, max_rows=100)
    assert sheets["csv"] == [{"name": "Alice", "note": ""}]


def test_merged_cell_reads_as_empty_in_later_rows():
    data = _xlsx_bytes({"Sheet1": [["Name", "Team"], ["Alice", "Eng"], ["Bob", None]]})
    sheets = read_sheets(data, "xlsx", budget=10_000_000, max_rows=100)
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
    sheets = read_sheets(data, "csv", budget=10_000, max_rows=100, sheet_name="History")
    assert list(sheets.keys()) == ["History"]
    assert sheets["History"] == [{"name": "Alice", "email": "alice@example.com"}]
