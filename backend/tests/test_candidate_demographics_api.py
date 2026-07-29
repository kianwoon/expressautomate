"""The facts a MOM form asks for, and the line they must never cross.

Two kinds of test live here, and the second kind is the important one.

The first kind is ordinary: sex, race, nationality, date of birth, years of
education and languages round-trip through the API, NULL means "not recorded",
and each database CHECK refuses a value outside its vocabulary. The CHECK tests
assert the *constraint name* from the `asyncpg` error rather than merely that
something failed — a test that only asserts "it raised" passes just as happily
when a NOT NULL or a foreign key fired instead, and would not notice a
constraint being dropped and a different one catching the write by accident.

The second kind guards the reason this platform can hold these columns at all:
nothing may feed them to a model, and nothing may filter on them. See
`app/services/sourcing/redact.py`. A candidate's race is a statutory deduction
category and a form field; the moment it reaches a prompt or a query parameter
it becomes a way to rank people, which is unlawful in Singapore and is the
failure mode this whole subsystem is shaped around.
"""

import uuid
from datetime import date

import pytest
from asyncpg.exceptions import CheckViolationError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.main import app
from app.services.sourcing.explain import MatchCandidate, build_prompt
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in

# The five columns that exist for a form and must never reach a model or a
# filter. Named once so a test cannot drift from the list it is guarding.
PROTECTED_COLUMNS = ("sex", "race", "race_detail", "nationality", "date_of_birth")


@pytest.fixture
async def agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    yield tid, uid
    async with AdminSessionLocal() as s:
        for table in ("candidate_languages", "candidate_skills", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


_RAW_INSERT = (
    "INSERT INTO candidates (id, tenant_id, full_name, record_status, pipeline_stage"
)


async def _insert_raw(tenant_id, column: str, value) -> None:
    """Write one column straight past Pydantic, so the CHECK is what answers."""
    async with tenant_session(tenant_id) as s:
        await s.execute(
            text(
                f"{_RAW_INSERT}, {column}) "
                "VALUES (:i, :t, 'Raw Row', 'active', 'new', :v)"
            ),
            {"i": uuid.uuid4(), "t": tenant_id, "v": value},
        )
        await s.commit()


def _constraint_of(exc: IntegrityError) -> str:
    """The constraint the database actually refused on.

    Reached through `.orig.__cause__` because SQLAlchemy wraps the asyncpg
    error twice: once in its own `AdaptedConnection` exception and once in
    `IntegrityError`. Asserting on this string is what makes these tests about
    a named rule rather than about "some write failed".
    """
    cause = exc.orig
    while cause is not None and not isinstance(cause, CheckViolationError):
        cause = getattr(cause, "__cause__", None)
    assert isinstance(cause, CheckViolationError), f"not a CHECK violation: {exc.orig!r}"
    return cause.constraint_name


# --- round trips ---------------------------------------------------------


async def test_every_new_field_round_trips(agency) -> None:
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        created = await http.post(
            "/api/candidates",
            json={
                "full_name": "Maria Santos",
                "sex": "female",
                "race": "others",
                "race_detail": "Filipino",
                "nationality": "PH",
                "date_of_birth": "1990-04-12",
                "education_years": 10,
                "languages": [
                    {"language": "Tagalog", "fluency": "native"},
                    {"language": "English", "fluency": "conversational"},
                ],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()

    assert body["sex"] == "female"
    assert body["race"] == "others"
    assert body["race_detail"] == "Filipino"
    assert body["nationality"] == "PH"
    assert body["date_of_birth"] == "1990-04-12"
    assert body["education_years"] == 10
    assert body["languages"] == [
        {"language": "English", "fluency": "conversational"},
        {"language": "Tagalog", "fluency": "native"},
    ]


async def test_absent_fields_are_null_not_guessed(agency) -> None:
    """A name that reads female must not produce `sex='female'` (§15)."""
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        body = (
            await http.post("/api/candidates", json={"full_name": "Siti Nurhaliza"})
        ).json()

    for column in PROTECTED_COLUMNS:
        assert body[column] is None, f"{column} was inferred from nothing"
    assert body["education_years"] is None
    assert body["languages"] == []


async def test_patch_updates_and_clears_each_field(agency) -> None:
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        cid = (
            await http.post(
                "/api/candidates",
                json={"full_name": "Nur Aisyah", "sex": "female", "nationality": "ID"},
            )
        ).json()["id"]

        patched = (
            await http.patch(
                f"/api/candidates/{cid}",
                json={
                    "race": "malay",
                    "date_of_birth": "1985-01-02",
                    "education_years": 8,
                    "nationality": None,
                },
            )
        ).json()

    assert patched["race"] == "malay"
    assert patched["date_of_birth"] == "1985-01-02"
    assert patched["education_years"] == 8
    # Explicitly sent null clears it, and clearing is not the same as omitting.
    assert patched["nationality"] is None
    assert patched["sex"] == "female"


async def test_lowercase_nationality_is_stored_uppercase(agency) -> None:
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        body = (
            await http.post(
                "/api/candidates", json={"full_name": "Ana Cruz", "nationality": "ph"}
            )
        ).json()
    assert body["nationality"] == "PH"


async def test_the_api_refuses_values_outside_each_vocabulary(agency) -> None:
    """422 with the field named, not a 500 out of the database."""
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        for field, bad in (
            ("sex", "unspecified"),
            ("race", "eurasian"),
            ("nationality", "PHL"),
            ("education_years", 31),
        ):
            r = await http.post(
                "/api/candidates", json={"full_name": "Bad Value", field: bad}
            )
            assert r.status_code == 422, f"{field}={bad!r} -> {r.status_code}"


# --- the CHECK constraints, by name --------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("sex", "unspecified", "ck_candidates_sex"),
        ("race", "eurasian", "ck_candidates_race"),
        ("nationality", "ph", "ck_candidates_nationality_iso_alpha2"),
        ("education_years", 31, "ck_candidates_education_years_range"),
        ("education_years", -1, "ck_candidates_education_years_range"),
    ],
)
async def test_each_check_constraint_refuses_by_name(
    agency, column, value, constraint
) -> None:
    tid, _uid = agency
    with pytest.raises(IntegrityError) as caught:
        await _insert_raw(tid, column, value)
    assert _constraint_of(caught.value) == constraint


async def test_the_fluency_check_refuses_by_name(agency) -> None:
    tid, _uid = agency
    cid = uuid.uuid4()
    async with tenant_session(tid) as s:
        await s.execute(
            text(f"{_RAW_INSERT}) VALUES (:i, :t, 'Speaker', 'active', 'new')"),
            {"i": cid, "t": tid},
        )
        await s.commit()

    with pytest.raises(IntegrityError) as caught:
        async with tenant_session(tid) as s:
            await s.execute(
                text(
                    "INSERT INTO candidate_languages "
                    "(id, tenant_id, candidate_id, language, language_normalized, fluency) "
                    "VALUES (:i, :t, :c, 'English', 'english', 'perfect')"
                ),
                {"i": uuid.uuid4(), "t": tid, "c": cid},
            )
            await s.commit()
    assert _constraint_of(caught.value) == "ck_candidate_languages_fluency"


# --- languages ------------------------------------------------------------


async def test_languages_replace_on_write_and_normalise_and_dedupe(agency) -> None:
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        cid = (
            await http.post(
                "/api/candidates",
                json={
                    "full_name": "Wati",
                    "languages": [
                        {"language": "  Bahasa Indonesia  ", "fluency": "native"},
                        # Same language, different spelling and case: one row
                        # survives, and it is the first one sent.
                        {"language": "bahasa   indonesia", "fluency": "basic"},
                        {"language": "English"},
                    ],
                },
            )
        ).json()["id"]

    # Deduped on the normalised form; the raw spelling is kept as typed.
    assert (await _languages_of(tid, cid)) == [
        # The first spelling sent wins, whitespace collapsed for the comparable
        # form and preserved-then-trimmed for the one the recruiter typed.
        ("Bahasa Indonesia", "bahasa indonesia", "native"),
        ("English", "english", None),
    ]

    async with await _client_for(tid, uid) as http:
        body = (
            await http.patch(
                f"/api/candidates/{cid}",
                json={"languages": [{"language": "Mandarin", "fluency": "fluent"}]},
            )
        ).json()

    # Replaced wholesale, not appended to.
    assert body["languages"] == [{"language": "Mandarin", "fluency": "fluent"}]

    async with await _client_for(tid, uid) as http:
        # Omitting the key leaves the set alone — the PATCH distinction that
        # `exclude_unset=True` exists for.
        body = (
            await http.patch(f"/api/candidates/{cid}", json={"notes": "spoke Tuesday"})
        ).json()
    assert body["languages"] == [{"language": "Mandarin", "fluency": "fluent"}]

    async with await _client_for(tid, uid) as http:
        body = (await http.patch(f"/api/candidates/{cid}", json={"languages": []})).json()
    assert body["languages"] == []


async def _languages_of(tenant_id, candidate_id) -> list[tuple]:
    async with tenant_session(tenant_id) as s:
        return [
            tuple(row)
            for row in (
                await s.execute(
                    text(
                        "SELECT language, language_normalized, fluency "
                        "FROM candidate_languages WHERE candidate_id = :c "
                        "ORDER BY language_normalized"
                    ),
                    {"c": uuid.UUID(str(candidate_id))},
                )
            ).all()
        ]


async def test_a_language_cannot_reference_another_agencys_candidate(agency) -> None:
    """§18, the same guard `test_candidate_isolation.py` puts on skills.

    RLS filters what a statement may read and write; it does not filter the
    referential-integrity check behind a foreign key. Only the composite FK
    carrying `tenant_id` stops agency B attaching a language to agency A's
    person.
    """
    tid, uid = agency
    other = uuid.uuid4()
    cid = uuid.uuid4()
    async with tenant_session(tid) as s:
        await s.execute(
            text(f"{_RAW_INSERT}) VALUES (:i, :t, 'Theirs', 'active', 'new')"),
            {"i": cid, "t": tid},
        )
        await s.commit()

    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": other, "n": f"agency-{other.hex[:6]}"},
        )
        await s.commit()
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(other) as s:
                await s.execute(
                    text(
                        "INSERT INTO candidate_languages "
                        "(id, tenant_id, candidate_id, language, language_normalized) "
                        "VALUES (:i, :t, :c, 'English', 'english')"
                    ),
                    {"i": uuid.uuid4(), "t": other, "c": cid},
                )
                await s.commit()
        # And it cannot read them either.
        async with tenant_session(other) as s:
            assert (
                await s.execute(text("SELECT id FROM candidate_languages"))
            ).all() == []
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(
                text("DELETE FROM candidate_languages WHERE tenant_id = :t"), {"t": other}
            )
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other})
            await s.commit()


# --- the guards ----------------------------------------------------------


def test_no_protected_attribute_reaches_a_sourcing_prompt() -> None:
    """THE guard test. A model must never be shown these facts about a person.

    `MatchCandidate` is a whitelist, so the way this breaks in practice is
    somebody adding a field to it "just for context". The assertion is made
    two ways on purpose: the *values* must be absent (a model that sees
    "Chinese" can reason on it whatever the label said), and the *keys* must
    be absent (a field labelled `race:` with an empty value still tells the
    model the axis exists and is worth reasoning about).

    See `app/services/sourcing/redact.py`.
    """
    # Values chosen to be unmistakable if they leak, and to appear nowhere in
    # the prompt's fixed text. An ISO alpha-2 code is deliberately not among
    # them — "ID" is a substring of `candidate_id`, so it can only be asserted
    # on structurally, which the `__dataclass_fields__` check below does.
    marker_values = ["female", "chinese", "indonesian", "1990-04-12"]

    class _Opportunity:
        job_title = "Domestic helper"
        job_description = "Household of four."
        requirements = "Cooking and childcare."

    prompt, _removed = build_prompt(
        _Opportunity(),
        [
            MatchCandidate(
                candidate_id="c1",
                full_name="Wati",
                current_title="Domestic helper",
                skills=["cooking"],
                cv_text="Cooked for a family of four.",
            )
        ],
    )
    lowered = prompt.lower()

    for column in PROTECTED_COLUMNS:
        assert f"{column}:" not in lowered, f"{column} is a field in the prompt"
    # `education_years` and languages are held to the same rule.
    assert "education_years:" not in lowered
    assert "languages:" not in lowered
    for value in marker_values:
        assert value.lower() not in lowered, f"{value} leaked into the prompt"

    # `MatchCandidate` itself carries no such field — the structural half of
    # the same claim, which survives a change to the prompt's wording.
    fields = set(MatchCandidate.__dataclass_fields__)
    assert fields.isdisjoint({*PROTECTED_COLUMNS, "education_years", "languages", "age"})


def test_the_cv_extraction_schema_asks_for_none_of_them() -> None:
    """The CV parser must not propose a protected characteristic.

    A CV for a domestic-worker placement states sex, date of birth and
    nationality on the first page. The schema below is what the model is told
    to return, and it asks for roles and skills only — so those facts are
    filled in by a person or not at all (§15).
    """
    from app.services.cv.extract import build_prompt as build_cv_prompt
    from app.services.cv.schema import CVResponse, cv_json_schema

    assert set(CVResponse.model_fields) == {"roles", "skills"}

    schema_and_prompt = (
        str(cv_json_schema()) + build_cv_prompt("Jane Tan, female, born 1990, Filipino.")
    ).lower()
    # The CV text itself is quoted into the prompt, so the assertion is about
    # the *asked-for fields*, not about words appearing anywhere.
    for banned in (
        '"sex"', '"race"', '"gender"', '"nationality"', '"date_of_birth"',
        '"dob"', '"age"', '"ethnicity"', '"religion"', '"marital_status"',
    ):
        assert banned not in str(cv_json_schema()).lower(), f"{banned} in the CV schema"
    assert "extract" in schema_and_prompt  # the prompt was actually built


# --- no filtering ---------------------------------------------------------


async def test_the_list_does_not_filter_on_any_protected_attribute(agency) -> None:
    """`?race=...` must silently no-op: not an error, and not a filter.

    An error would advertise that the parameter is understood and merely
    spelled wrong. Filtering would be the platform shortlisting on a protected
    characteristic. Returning everything is the only correct answer, and it is
    what FastAPI does with a parameter no endpoint declares — this test is what
    stops somebody declaring one.
    """
    tid, uid = agency
    async with await _client_for(tid, uid) as http:
        for payload in (
            {"full_name": "Chen Wei", "race": "chinese", "sex": "female"},
            {"full_name": "Rahmat", "race": "malay", "sex": "male"},
            {"full_name": "Priya", "race": "indian", "sex": "female"},
        ):
            assert (await http.post("/api/candidates", json=payload)).status_code == 201

        everyone = (await http.get("/api/candidates")).json()["total"]
        assert everyone == 3

        for query in (
            "race=chinese",
            "sex=female",
            "nationality=SG",
            "date_of_birth=1990-04-12",
            "education_years=8",
            "language=english",
            "languages=english",
        ):
            r = await http.get(f"/api/candidates?{query}")
            assert r.status_code == 200, f"{query} -> {r.status_code}"
            assert r.json()["total"] == everyone, f"{query} filtered the list"


async def test_nothing_computes_or_persists_an_age(agency) -> None:
    """The date is stored as given, and no age is derived from it anywhere.

    `date_of_birth` was chosen over an age column precisely so that no stored
    number can go stale (see the column comment). A derived `age` appearing in
    the payload — or a column holding one — would give that back.
    """
    tid, uid = agency
    born = "1990-04-12"
    async with await _client_for(tid, uid) as http:
        body = (
            await http.post(
                "/api/candidates",
                json={"full_name": "Sri Wahyuni", "date_of_birth": born},
            )
        ).json()
        cid = body["id"]
        fetched = (await http.get(f"/api/candidates/{cid}")).json()
        exported = (await http.get(f"/api/candidates/{cid}/export")).json()

    assert body["date_of_birth"] == born
    assert fetched["date_of_birth"] == born
    assert exported["date_of_birth"] == born
    # Stored verbatim, not shifted by a timezone on the way through.
    async with tenant_session(tid) as s:
        stored = (
            await s.execute(
                text("SELECT date_of_birth FROM candidates WHERE id = :i"),
                {"i": uuid.UUID(cid)},
            )
        ).scalar_one()
    assert stored == date.fromisoformat(born)

    for payload in (body, fetched, exported):
        assert "age" not in payload

    # And no column anywhere holds one.
    async with AdminSessionLocal() as s:
        age_columns = (
            await s.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name IN ('age', 'age_years')"
                )
            )
        ).all()
    assert age_columns == []
