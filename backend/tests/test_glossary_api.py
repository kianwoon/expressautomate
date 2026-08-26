"""The glossary is per-agency data, and these tests are about why that matters.

allow-hardcode: this is a test module. Every literal below is a fixture value
or an expected result — the thing the assertions are checking against. There is
no matching oracle here; the codes named (`C/F`, `C/O`, `PR`) are drawn from the
starter list under test, and `STARTER_CODES` is imported rather than restated so
the suite cannot drift from what ships.

Three properties, in descending order of how badly a regression would hurt:

- **Agency A cannot read, edit or delete Agency B's codes.** A glossary names
  the clients' own shorthand and the protected characteristics they hire on.
  Leaking it is worse than leaking a vacancy.
- **One normalised code, one meaning.** `C/F`, `c/f` and `C / F` all match the
  same email text, so if all three could be stored the agency would have three
  definitions competing and no way to see which one the detector used.
- **A deleted starter stays deleted.** The starter set is seeded lazily and
  re-checked on every read, so the ledger that stops resurrection is the only
  thing between "sensible default" and "the delete button does not work".
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.api.glossary import STARTER_CODES
from app.core.config import settings
from app.models import User
from app.models.glossary import PROTECTED_ATTRIBUTES, normalise
from tests.conftest import AdminSessionLocal

# Glossary is global seed data; these tests assert counts that only hold when
# no other file writes concurrently — run serially in CI.
pytestmark = pytest.mark.serial


@pytest.fixture(autouse=True)
def settings_the_suite_supplies(monkeypatch) -> None:
    """CI has no `.env`, so the suite states every value it depends on.

    Unconditional rather than a fallback: seeding is on in every test here
    because the seed ledger is what most of them are about, and inheriting the
    developer machine's `.env` would let a local `false` quietly skip them.
    """
    monkeypatch.setattr(settings, "GLOSSARY_SEED_STARTERS", True)
    monkeypatch.setattr(settings, "GLOSSARY_CODE_MAX_LENGTH", 32)
    monkeypatch.setattr(settings, "GLOSSARY_MEANING_MAX_LENGTH", 500)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """ASGI transport, not TestClient: TestClient drives its own event loop and
    the engine in app.db.session is pinned to the session-scoped one."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
async def agencies():
    """Two agencies with a signed-in user each.

    Seeded through the admin role because RLS is the thing under test: fixtures
    written through the restricted role would prove isolation by never having
    inserted the other tenant's rows in the first place.
    """
    made: list[uuid.UUID] = []

    async def make(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
        tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
        async with AdminSessionLocal() as s:
            await s.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
                {"i": tenant_id, "n": slug, "s": f"{slug}-{tenant_id.hex[:8]}"},
            )
            # The ORM, not raw SQL: `users.role` is NOT NULL with a Python-side
            # default that a hand-written INSERT never fires.
            s.add(User(id=user_id, tenant_id=tenant_id, email=f"{tenant_id.hex[:8]}@{slug}.sg"))
            await s.commit()
        made.append(tenant_id)
        return tenant_id, user_id

    yield make

    async with AdminSessionLocal() as s:
        for tenant_id in made:
            await s.execute(
                text("DELETE FROM glossary_codes WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await s.execute(
                text("DELETE FROM glossary_seed_marks WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tenant_id})
            await s.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})
        await s.commit()


def _cookies(tenant_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
    return {
        SESSION_COOKIE: _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)})
    }


async def _codes(client, cookies) -> dict[str, dict]:
    response = await client.get("/api/glossary", cookies=cookies)
    assert response.status_code == 200
    return {c["code"]: c for c in response.json()["codes"]}


# --- normalisation -----------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    ["C/F", "c/f", "C / F", " c / f ", "C.F.", "cF", "C-F"],
)
def test_normalise_collapses_spelling(written: str) -> None:
    """Every spelling a client might use for one code folds to one key.

    Parameterised over the real variants rather than asserting the output
    string once, because the failure this guards against is a *new* separator
    slipping through — and a single `assert normalise("C/F") == "cf"` would
    still pass while `C-F` became its own code.
    """
    assert normalise(written) == "cf"


def test_normalise_keeps_non_ascii_letters() -> None:
    """Folding is Unicode-wide, and two codes that differ outside ASCII stay distinct.

    Pinned because the tempting implementation — an ASCII-only character class
    — is not a narrower rule but a wrong one. It deletes the `é` rather than
    keeping it, so `Bé/F` collapses onto `B/F` and the second agency code an
    operator adds is refused with a 409 naming a code that does not look like
    theirs. A code written in another script fares worse still, folding to
    almost nothing and colliding with everything that folds the same way.
    """
    assert normalise("Bé/F") == "béf"
    assert normalise("BÉ/F") == normalise("bé / f") == "béf"
    assert normalise("Bé/F") != normalise("B/F")
    # A code with no Latin characters at all survives as itself.
    assert normalise("中/F") == "中f"


def test_normalise_rejects_punctuation_only() -> None:
    """A code of pure punctuation must not fold to something matching everything."""
    assert normalise("///") == ""


def test_starter_codes_do_not_collide() -> None:
    """The shipped list must itself satisfy the uniqueness it is inserted under.

    Not a tautology: two starter entries that normalise alike would make the
    seed insert drop one silently via ON CONFLICT DO NOTHING, and the missing
    code would look like a seeding bug rather than a bad list.
    """
    keys = [normalise(code) for code, _m, _a, _n in STARTER_CODES]
    assert len(keys) == len(set(keys))
    assert all(keys)
    for _code, _meaning, attribute, _notes in STARTER_CODES:
        assert attribute is None or attribute in PROTECTED_ATTRIBUTES


# --- reading -----------------------------------------------------------------


async def test_first_read_seeds_starters_and_offers_attributes(client, agencies) -> None:
    tenant_id, user_id = await agencies("alpha")

    response = await client.get("/api/glossary", cookies=_cookies(tenant_id, user_id))

    assert response.status_code == 200
    body = response.json()
    assert body["attributes"] == list(PROTECTED_ATTRIBUTES)
    by_code = {c["code"]: c for c in body["codes"]}
    assert len(body["codes"]) == len(STARTER_CODES)
    # Ours until they say otherwise — the whole point of the `source` column.
    assert all(c["source"] == "starter" for c in body["codes"])
    expected = {code: attribute for code, _m, attribute, _n in STARTER_CODES}
    assert {code: row["attribute"] for code, row in by_code.items()} == expected


async def test_reading_twice_does_not_duplicate(client, agencies) -> None:
    """Seeding runs on every read, so it has to be idempotent by construction."""
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)

    first = await _codes(client, cookies)
    second = await _codes(client, cookies)

    assert len(second) == len(first) == len(STARTER_CODES)


async def test_unauthenticated_read_is_refused(client) -> None:
    assert (await client.get("/api/glossary")).status_code == 401


# --- writing -----------------------------------------------------------------


async def test_create_and_duplicate_names_the_existing_meaning(client, agencies) -> None:
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)

    created = await client.post(
        "/api/glossary",
        json={"code": "P/T", "meaning": "Part time", "notes": "Client shorthand."},
        cookies=cookies,
    )
    assert created.status_code == 201
    assert created.json()["source"] == "agency"

    # A different spelling of the same code. Rejected, and the message has to
    # carry the existing meaning or the operator cannot tell what they clashed
    # with — the code they typed is not the code shown in the list.
    clash = await client.post(
        "/api/glossary", json={"code": "p / t", "meaning": "Permanent transfer"}, cookies=cookies
    )
    assert clash.status_code == 409
    assert "Part time" in clash.json()["detail"]


async def test_punctuation_only_code_is_refused(client, agencies) -> None:
    tenant_id, user_id = await agencies("alpha")
    response = await client.post(
        "/api/glossary",
        json={"code": "///", "meaning": "Anything"},
        cookies=_cookies(tenant_id, user_id),
    )
    assert response.status_code == 400


async def test_unknown_attribute_is_refused(client, agencies) -> None:
    """The CHECK constraint would otherwise reject this as a 500."""
    tenant_id, user_id = await agencies("alpha")
    response = await client.post(
        "/api/glossary",
        json={"code": "ZZ", "meaning": "Something", "attribute": "star_sign"},
        cookies=_cookies(tenant_id, user_id),
    )
    assert response.status_code == 400


async def test_two_codes_differing_only_outside_ascii_are_distinct(client, agencies) -> None:
    """The end-to-end consequence of the folding rule above.

    Through the API, because the 409 is where an ASCII-only fold would surface
    — as a refusal to add a legitimate second code.
    """
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)

    first = await client.post(
        "/api/glossary", json={"code": "B/F", "meaning": "Bilingual, female"}, cookies=cookies
    )
    second = await client.post(
        "/api/glossary", json={"code": "Bé/F", "meaning": "Something else entirely"},
        cookies=cookies,
    )

    assert first.status_code == 201
    assert second.status_code == 201


async def test_patch_cannot_blank_a_meaning(client, agencies) -> None:
    """A whitespace-only meaning is rejected, not stored as "".

    A code with no meaning is worse than a deleted one: the detector still
    matches it and decodes the shorthand to nothing, so the job order loses a
    requirement with no glossary row that looks wrong.
    """
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)
    codes = await _codes(client, cookies)
    before = codes["PR"]["meaning"]

    blanked = await client.patch(
        f"/api/glossary/{codes['PR']['id']}", json={"meaning": "   "}, cookies=cookies
    )

    assert blanked.status_code == 400
    after = await _codes(client, cookies)
    assert after["PR"]["meaning"] == before


async def test_editing_a_starter_adopts_it(client, agencies) -> None:
    """An edited default stops being ours. So does a confirmed one."""
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)
    codes = await _codes(client, cookies)
    ambiguous = codes["C/O"]

    edited = await client.patch(
        f"/api/glossary/{ambiguous['id']}",
        json={"meaning": "Care of", "attribute": None},
        cookies=cookies,
    )
    assert edited.status_code == 200
    assert edited.json() == {
        **ambiguous,
        "meaning": "Care of",
        "attribute": None,
        "source": "agency",
    }

    # A PATCH that changes nothing is the explicit "this default is right for
    # us" the settings screen needs.
    confirmed = await client.patch(
        f"/api/glossary/{codes['PR']['id']}", json={}, cookies=cookies
    )
    assert confirmed.json()["source"] == "agency"
    assert confirmed.json()["meaning"] == codes["PR"]["meaning"]


async def test_an_edited_starter_is_not_overwritten_by_a_later_read(client, agencies) -> None:
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)
    codes = await _codes(client, cookies)
    await client.patch(
        f"/api/glossary/{codes['C/O']['id']}", json={"meaning": "Care of"}, cookies=cookies
    )

    after = await _codes(client, cookies)

    assert after["C/O"]["meaning"] == "Care of"
    assert after["C/O"]["source"] == "agency"


async def test_a_deleted_starter_stays_deleted(client, agencies) -> None:
    """The property the seed ledger exists for.

    Without the ledger this passes on the delete and fails on the next read —
    which is the shape of the bug that makes an agency believe the product
    ignores them.
    """
    tenant_id, user_id = await agencies("alpha")
    cookies = _cookies(tenant_id, user_id)
    codes = await _codes(client, cookies)

    deleted = await client.delete(f"/api/glossary/{codes['C/O']['id']}", cookies=cookies)
    assert deleted.status_code == 204

    # Two further reads, not one: a ledger written outside the seeding
    # transaction could still survive the first and fail the second.
    for _ in range(2):
        after = await _codes(client, cookies)
        assert "C/O" not in after
        assert len(after) == len(STARTER_CODES) - 1


# --- tenant isolation --------------------------------------------------------


async def test_one_agency_cannot_read_anothers_codes(client, agencies) -> None:
    alpha_tenant, alpha_user = await agencies("alpha")
    beta_tenant, beta_user = await agencies("beta")
    await client.post(
        "/api/glossary",
        json={"code": "ACME", "meaning": "Acme Manufacturing Pte Ltd"},
        cookies=_cookies(alpha_tenant, alpha_user),
    )

    beta_codes = await _codes(client, _cookies(beta_tenant, beta_user))

    assert "ACME" not in beta_codes


async def test_one_agency_cannot_edit_or_delete_anothers_code(client, agencies) -> None:
    """404, not 403 — RLS makes the row invisible, and that is the right answer.

    Distinguishing "not yours" from "does not exist" would confirm the id
    belongs to somebody, which is itself a leak.
    """
    alpha_tenant, alpha_user = await agencies("alpha")
    beta_tenant, beta_user = await agencies("beta")
    created = await client.post(
        "/api/glossary",
        json={"code": "ACME", "meaning": "Acme Manufacturing Pte Ltd"},
        cookies=_cookies(alpha_tenant, alpha_user),
    )
    code_id = created.json()["id"]
    beta = _cookies(beta_tenant, beta_user)

    assert (
        await client.patch(f"/api/glossary/{code_id}", json={"meaning": "Hijacked"}, cookies=beta)
    ).status_code == 404
    assert (await client.delete(f"/api/glossary/{code_id}", cookies=beta)).status_code == 404

    # And the row is untouched, read back through its owner.
    alpha_codes = await _codes(client, _cookies(alpha_tenant, alpha_user))
    assert alpha_codes["ACME"]["meaning"] == "Acme Manufacturing Pte Ltd"


async def test_the_same_code_may_mean_different_things_to_two_agencies(client, agencies) -> None:
    """The reason this is not a global table."""
    alpha_tenant, alpha_user = await agencies("alpha")
    beta_tenant, beta_user = await agencies("beta")

    for tenant_id, user_id, meaning in (
        (alpha_tenant, alpha_user, "Chinese, gender open"),
        (beta_tenant, beta_user, "Care of"),
    ):
        cookies = _cookies(tenant_id, user_id)
        codes = await _codes(client, cookies)
        await client.patch(
            f"/api/glossary/{codes['C/O']['id']}", json={"meaning": meaning}, cookies=cookies
        )

    alpha = await _codes(client, _cookies(alpha_tenant, alpha_user))
    beta = await _codes(client, _cookies(beta_tenant, beta_user))
    assert alpha["C/O"]["meaning"] == "Chinese, gender open"
    assert beta["C/O"]["meaning"] == "Care of"


async def test_rls_hides_glossary_rows_from_an_unscoped_session(agencies) -> None:
    """Belt and braces on the policy itself, not on the endpoint.

    The API always sets `app.tenant_id`; this asserts what happens when
    something does not — a worker, a future endpoint, a REPL. The answer must
    be zero rows, never every agency's.
    """
    from app.db.session import SessionLocal

    tenant_id, _user_id = await agencies("alpha")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO glossary_codes"
                " (id, tenant_id, code, code_normalised, meaning, source)"
                " VALUES (:i, :t, 'ACME', 'acme', 'Acme Pte Ltd', 'agency')"
            ),
            {"i": uuid.uuid4(), "t": tenant_id},
        )
        await s.commit()

    async with SessionLocal() as s:
        rows = (await s.execute(text("SELECT count(*) FROM glossary_codes"))).scalar_one()

    assert rows == 0, "an unscoped session must see no glossary rows at all"
