"""Tests for `app.services.imports.table` — bytes to rows, nothing more."""

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
    assert sniff_table(buf.getvalue()) != "xlsx"


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
