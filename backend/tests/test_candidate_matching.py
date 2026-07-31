"""Which existing person a set of details refers to, if any.

The matcher only ever *reads*. It reports what it found — including that it
found two different people — and the caller decides what to write. That split
is what makes the same function usable from a manual POST and, later, from a
bulk import that must record an outcome per row.
"""

import re
import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.candidate_matching import abbreviate, find_candidate
from tests.conftest import AdminSessionLocal

_LONG_DIGIT_RUN = re.compile(r"\d{4,}")

_INSERT = text(
    "INSERT INTO candidates (id, tenant_id, full_name, email, phone_e164, "
    "record_status, pipeline_stage) "
    "VALUES (:i, :t, :n, :e, :p, :s, 'new')"
)


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        # Clear the merge pointers before deleting: the CHECK requires a merged
        # row to name a target, so status and target must be cleared together,
        # and the self-FK blocks deleting a target while a loser points at it.
        await s.execute(
            text(
                "UPDATE candidates SET record_status = 'active', "
                "merged_into_candidate_id = NULL WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
        for table in ("candidate_field_overrides", "candidate_skills", "candidates"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _seed(tenant_id, *, name="Jane Tan", email=None, phone=None, status="active"):
    cid = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        await s.execute(
            _INSERT,
            {"i": cid, "t": tenant_id, "n": name, "e": email, "p": phone, "s": status},
        )
        await s.commit()
    return cid


async def test_email_alone_matches(agency) -> None:
    cid = await _seed(agency, email="jane@acme.sg")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane@acme.sg", None)
    assert result.candidate_id == cid
    assert result.matched_on == "email"


async def test_phone_alone_matches(agency) -> None:
    cid = await _seed(agency, phone="+6591234567")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, None, "+6591234567")
    assert result.candidate_id == cid
    assert result.matched_on == "phone"


async def test_a_changed_email_still_matches_on_the_unchanged_mobile(agency) -> None:
    """The case the whole either-key rule exists for."""
    cid = await _seed(agency, email="jane@gmail.com", phone="+6591234567")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane.tan@newco.sg", "+6591234567")
    assert result.candidate_id == cid


async def test_email_is_matched_case_insensitively(agency) -> None:
    cid = await _seed(agency, email="jane@acme.sg")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "JANE@ACME.SG", None)
    assert result.candidate_id == cid


async def test_nothing_to_match_on_finds_nobody(agency) -> None:
    await _seed(agency, email="jane@acme.sg")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, None, None)
    assert result.candidate_id is None
    assert result.conflict is None


async def test_a_split_identity_is_a_conflict_not_a_guess(agency) -> None:
    """Email says one person, phone says another. Both answers would be wrong."""
    a = await _seed(agency, name="Jane Tan", email="jane@acme.sg")
    b = await _seed(agency, name="John Lim", phone="+6591234567")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane@acme.sg", "+6591234567")
    assert result.candidate_id is None
    assert result.conflict is not None
    assert set(result.conflict) == {a, b}


async def test_an_archived_candidate_still_matches(agency) -> None:
    """They still hold the unique key; skipping them would collide on insert."""
    cid = await _seed(agency, email="jane@acme.sg", status="archived")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane@acme.sg", None)
    assert result.candidate_id == cid


async def test_a_merged_candidate_is_not_returned(agency) -> None:
    """A merged row's identity belongs to its target, so it is not a match."""
    survivor = await _seed(agency, email="survivor@acme.sg")
    loser = await _seed(agency, email="loser@acme.sg")
    async with tenant_session(agency) as s:
        await s.execute(
            text(
                "UPDATE candidates SET record_status = 'merged', "
                "merged_into_candidate_id = :w WHERE id = :l"
            ),
            {"w": survivor, "l": loser},
        )
        await s.commit()
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "loser@acme.sg", None)
    assert result.candidate_id is None


class TestAbbreviate:
    """The bound must be structural: no `@`, no 4+ digit run, ever — the
    content is untrusted even though the shape is safe by construction."""

    def _assert_safe(self, result: str) -> None:
        assert result != ""
        assert "@" not in result
        assert not _LONG_DIGIT_RUN.search(result)

    def test_an_ordinary_multi_token_name(self) -> None:
        result = abbreviate("Wei Ming Tan")
        assert result == "Wei Ming T."
        self._assert_safe(result)

    def test_a_single_token_ordinary_name(self) -> None:
        result = abbreviate("Cher")
        self._assert_safe(result)

    def test_a_single_token_name_that_is_an_email_address(self) -> None:
        result = abbreviate("weiming@example.com")
        self._assert_safe(result)

    def test_a_name_containing_a_phone_number(self) -> None:
        result = abbreviate("John Tan 9123 4567")
        self._assert_safe(result)

    def test_a_very_long_many_token_name(self) -> None:
        result = abbreviate(" ".join(f"Token{i}" for i in range(50)))
        self._assert_safe(result)

    def test_whitespace_only(self) -> None:
        result = abbreviate("   ")
        self._assert_safe(result)
