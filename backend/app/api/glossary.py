"""The agency's own dictionary of client shorthand.

Four endpoints over one table, and the interesting part is not the CRUD — it
is the seeding.

A brand-new agency with an empty glossary learns nothing from the screen, so
the product ships a starter set of Singapore recruitment shorthand. But a
starter meaning that is wrong for *this* agency is worse than a missing one:
it silently decodes a client's requirement into something the client did not
ask for, and nothing on the job order says a default was involved. So a
starter row stays visibly ours (`source = "starter"`) until an operator edits
or confirms it, at which point it becomes theirs.

Seeding happens on first read rather than in the migration. A migration can
only reach the tenants that exist when it runs, so a migration-seeded starter
set needs a second mechanism for every tenant created afterwards — two code
paths that have to agree about the same list, which is how a starter set ends
up different depending on when an agency signed up. Doing it on read is one
path for both, and it costs one indexed lookup on a screen nobody opens in a
loop.

Re-seeding is safe because `glossary_seed_marks` records what has been
offered, not what is currently present. A deleted starter has a mark and no
row, and the seed pass reads the mark — so the deletion survives. An edited
starter has a mark too, so nothing overwrites it. A code added to the starter
list in a later release has no mark anywhere and reaches every existing tenant
exactly once.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _require_session
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.glossary import (
    PROTECTED_ATTRIBUTES,
    SOURCE_AGENCY,
    SOURCE_STARTER,
    GlossaryCode,
    GlossarySeedMark,
    normalise,
)

log = get_logger(__name__)
router = APIRouter(tags=["glossary"])


# allow-hardcode. This list is *seed data*, not matching logic: it is copied
# into the tenant's own table on first read and is thereafter editable and
# deletable like anything they typed. Nothing in the detection path ever reads
# this tuple, so a wrong entry here is a wrong default an agency can correct in
# one click — not a rule baked into the binary. It is source rather than
# configuration because it is a product decision about what a Singapore agency
# should find on the screen, versioned with the code that shipped it, and an
# operator editing a JSON file to change one meaning would be editing it for
# every tenant at once — the exact global-dictionary failure this feature
# exists to avoid.
#
# `attribute` names the single protected characteristic the code most directly
# references. Several codes reference two (`C/F` is race *and* gender); the
# column is singular by contract, so the secondary one is stated in `notes`
# where a human reads it rather than dropped.
#
# The race/gender codes are a positional grammar, not a list of unrelated
# tokens: the first letter states race and the second states gender, so the
# set is a cross product and is written out in full. Written out rather than
# generated, because each row is an editable record an agency can correct, and
# because the grammar has one trap that a generator would hide —
#
#   **M means Malay in the first position and Male in the second.**
#
# `M/M` is therefore "Malay, male", and it is exactly the code an incomplete
# list would omit and leave a recruiter to guess at. Every combination is
# present for that reason; a missing one is not a neutral absence, it is a
# code that silently fails to decode while its neighbours work.
STARTER_CODES: tuple[tuple[str, str, str | None, str | None], ...] = (
    # (code, meaning, attribute, notes)
    # First letter: C Chinese · M Malay · I Indian · E Eurasian · O open.
    # Second letter: F female · M male · O open.
    ("C/F", "Chinese, female", "race", "Also states gender: female."),
    ("C/M", "Chinese, male", "race", "Also states gender: male."),
    ("C/O", "Chinese, any gender", "race", "Ambiguous — some clients write this for 'care of'."),
    ("M/F", "Malay, female", "race", "Also states gender: female."),
    ("M/M", "Malay, male", "race", "The M's differ: Malay first, male second."),
    ("M/O", "Malay, any gender", "race", None),
    ("I/F", "Indian, female", "race", "Also states gender: female."),
    ("I/M", "Indian, male", "race", "Also states gender: male."),
    ("I/O", "Indian, any gender", "race", None),
    ("E/F", "Eurasian, female", "race", "Also states gender: female."),
    ("E/M", "Eurasian, male", "race", "Also states gender: male."),
    ("E/O", "Eurasian, any gender", "race", None),
    # `O` is **open**, not "Others". Singapore's CMIO convention uses O for
    # Others — everyone outside Chinese, Malay and Indian — and the two
    # readings are opposites: Others excludes the three largest groups, open
    # excludes nobody. Confirmed with the agency as open. It matters because
    # under the wrong reading a restriction would decode as its absence, and
    # the protected-attribute flag would stay silent on the one job order that
    # most needed it.
    ("O/O", "Open — any race, any gender", None, "No preference stated. O is open, not 'Others'."),
    ("O/F", "Any race, female", "gender", None),
    ("O/M", "Any race, male", "gender", None),
    ("SC", "Singapore Citizen", "nationality", None),
    ("PR", "Singapore Permanent Resident", "nationality", None),
    ("SPR", "Singapore Permanent Resident", "nationality", None),
    ("EP", "Employment Pass holder", "nationality", None),
    ("SP", "S Pass holder", "nationality", None),
    ("WP", "Work Permit holder", "nationality", None),
    ("DP", "Dependant's Pass holder", "nationality", None),
    ("LOC", "Letter of Consent", "nationality", None),
    ("YOE", "Years of experience", None, None),
    ("JD", "Job description", None, None),
    ("MNC", "Multinational corporation", None, None),
    ("SME", "Small or medium enterprise", None, None),
    ("OT", "Overtime", None, None),
    ("ASAP", "As soon as possible", None, None),
    ("5.5D", "Five-and-a-half-day work week", None, None),
    ("6D", "Six-day work week", None, None),
    ("N/A", "Not applicable", None, None),
)


class CodeCreate(BaseModel):
    code: str = Field(min_length=1)
    meaning: str = Field(min_length=1)
    attribute: str | None = None
    notes: str | None = None


class CodeUpdate(BaseModel):
    """Every field optional — a PATCH that names none of them is a confirm.

    The frontend needs a way to say "this starter default is right for us"
    without inventing an edit, because forcing a cosmetic change to clear the
    "ours, not yours" marker would make the marker meaningless.
    """

    meaning: str | None = Field(default=None, min_length=1)
    attribute: str | None = None
    notes: str | None = None


def _row(code: GlossaryCode) -> dict:
    return {
        "id": str(code.id),
        "code": code.code,
        "meaning": code.meaning,
        "attribute": code.attribute,
        "source": code.source,
        "notes": code.notes,
    }


def _validated(code: str, meaning: str, attribute: str | None) -> tuple[str, str]:
    """Reject at the edge what the CHECK constraints would reject in the database.

    Not merely a nicer error: an integrity error inside `tenant_session` rolls
    the transaction back and surfaces as a 500, so an operator typing an empty
    code would be told the service is broken rather than that the code is.
    """
    code = code.strip()
    meaning = meaning.strip()
    if len(code) > settings.GLOSSARY_CODE_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"A code may be at most {settings.GLOSSARY_CODE_MAX_LENGTH} characters.",
        )
    if len(meaning) > settings.GLOSSARY_MEANING_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"A meaning may be at most {settings.GLOSSARY_MEANING_MAX_LENGTH} characters.",
        )
    if not meaning:
        raise HTTPException(status_code=400, detail="Give the code a meaning.")
    if not normalise(code):
        # "///" normalises to the empty string, which would match every code.
        raise HTTPException(
            status_code=400,
            detail="A code needs at least one letter or digit — punctuation alone is not a code.",
        )
    if attribute is not None and attribute not in PROTECTED_ATTRIBUTES:
        raise HTTPException(
            status_code=400,
            detail=f"Choose one of: {', '.join(PROTECTED_ATTRIBUTES)}.",
        )
    return code, meaning


async def _seed_starters(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Offer this tenant every starter code it has not been offered before.

    Both inserts are `ON CONFLICT DO NOTHING` on the unique (tenant_id, code)
    pairs. That is not belt-and-braces for the ledger — it is the race: two
    browser tabs opening the glossary at once run this concurrently, and
    without it one of them raises a unique violation and shows an error on a
    page that had in fact just been seeded correctly.
    """
    if not settings.GLOSSARY_SEED_STARTERS:
        return

    already = set(
        (
            await session.execute(
                select(GlossarySeedMark.code_normalised).where(
                    GlossarySeedMark.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    pending = [
        (code, meaning, attribute, notes)
        for code, meaning, attribute, notes in STARTER_CODES
        if normalise(code) not in already
    ]
    if not pending:
        return

    await session.execute(
        pg_insert(GlossaryCode)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "code": code,
                    "code_normalised": normalise(code),
                    "meaning": meaning,
                    "attribute": attribute,
                    "notes": notes,
                    "source": SOURCE_STARTER,
                }
                for code, meaning, attribute, notes in pending
            ]
        )
        .on_conflict_do_nothing(constraint="uq_glossary_codes_tenant_code")
    )
    # Written in the same transaction as the rows themselves. Marking before
    # the insert commits would let a rollback leave a tenant permanently
    # marked as seeded with nothing seeded — a glossary that is empty forever
    # and cannot be repaired without touching the database by hand.
    await session.execute(
        pg_insert(GlossarySeedMark)
        .values(
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "code_normalised": normalise(code),
                }
                for code, _meaning, _attribute, _notes in pending
            ]
        )
        .on_conflict_do_nothing(constraint="uq_glossary_seed_marks_tenant_code")
    )
    log.info("glossary_starters_seeded", tenant_id=str(tenant_id), count=len(pending))


@router.get("/glossary")
async def list_codes(request: Request) -> dict:
    """Every code this agency holds, plus the attributes it may choose from.

    `attributes` travels with the list so the settings screen does not carry
    its own copy of the enumeration. A frontend that hardcoded it would drift
    from the CHECK constraint, and the symptom would be a dropdown option that
    saves as a 400 on one value and nothing else.
    """
    _user_id, tenant_id = _require_session(request)

    async with tenant_session(tenant_id) as session:
        await _seed_starters(session, tenant_id)
        codes = (
            (
                await session.execute(
                    select(GlossaryCode)
                    .where(GlossaryCode.tenant_id == tenant_id)
                    # By normalised form, so `C/F` and `c/f` would sort
                    # adjacently if both somehow existed, and the order does
                    # not depend on how the operator capitalised things.
                    .order_by(GlossaryCode.code_normalised)
                )
            )
            .scalars()
            .all()
        )

    return {
        "codes": [_row(c) for c in codes],
        "attributes": list(PROTECTED_ATTRIBUTES),
    }


@router.post("/glossary", status_code=201)
async def create_code(request: Request, body: CodeCreate) -> dict:
    """Add one code. 409 names the meaning already on file.

    Naming it matters. The duplicate is by *normalised* form, so an operator
    typing `c/f` when `C/F` exists sees a rejection for a code that does not
    look like the one they typed — without the existing meaning in the message
    that reads as a bug rather than as "you already have this".
    """
    user_id, tenant_id = _require_session(request)
    code, meaning = _validated(body.code, body.meaning, body.attribute)
    normalised = normalise(code)

    async with tenant_session(tenant_id) as session:
        existing = (
            await session.execute(
                select(GlossaryCode).where(
                    GlossaryCode.tenant_id == tenant_id,
                    GlossaryCode.code_normalised == normalised,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"“{existing.code}” already means “{existing.meaning}”. "
                    "Edit that entry instead — the two spellings match the same text."
                ),
            )
        row = GlossaryCode(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            code=code,
            code_normalised=normalised,
            meaning=meaning,
            attribute=body.attribute,
            notes=(body.notes or None),
            source=SOURCE_AGENCY,
            created_by=user_id,
        )
        session.add(row)
        await session.flush()
        payload = _row(row)

    log.info("glossary_code_created", tenant_id=str(tenant_id), code=normalised)
    return payload


@router.patch("/glossary/{code_id}")
async def update_code(request: Request, code_id: uuid.UUID, body: CodeUpdate) -> dict:
    """Edit a meaning, attribute or note — and adopt the row while doing it.

    The code itself is not editable. Changing it is indistinguishable from
    deleting one entry and creating another, and doing it in place would move
    a definition onto shorthand that may already have its own meaning, with
    the 409 that protects against exactly that bypassed.

    Any successful PATCH sets `source` to "agency". Editing a starter *is* the
    act of taking responsibility for it, and leaving it marked as ours after
    the operator has rewritten it would make the marker lie in the direction
    that matters least — but a PATCH that changes nothing is the explicit
    "yes, this default is right for us" the UI needs.
    """
    _user_id, tenant_id = _require_session(request)

    if body.attribute is not None and body.attribute not in PROTECTED_ATTRIBUTES:
        raise HTTPException(
            status_code=400, detail=f"Choose one of: {', '.join(PROTECTED_ATTRIBUTES)}."
        )
    fields = body.model_fields_set
    meaning: str | None = None
    if body.meaning is not None:
        # Stripped *before* the emptiness check, exactly as create does it.
        # Pydantic's `min_length=1` passes a single space, so checking only the
        # raw value let a meaning of "   " through and stored "" — and a code
        # with no meaning is worse than a deleted one: it still matches the
        # email text and decodes it to nothing, so the shorthand silently
        # vanishes from the job order with no glossary entry to blame.
        meaning = body.meaning.strip()
        if not meaning:
            raise HTTPException(status_code=400, detail="Give the code a meaning.")
        if len(meaning) > settings.GLOSSARY_MEANING_MAX_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"A meaning may be at most "
                    f"{settings.GLOSSARY_MEANING_MAX_LENGTH} characters."
                ),
            )

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                select(GlossaryCode).where(
                    GlossaryCode.id == code_id, GlossaryCode.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # The RLS policy already makes another agency's row invisible, so
            # this is a 404 for both "no such code" and "not yours" — which is
            # the right answer to the second question too.
            raise HTTPException(status_code=404, detail="No such code.")

        if meaning is not None:
            row.meaning = meaning
        # `attribute` and `notes` are clearable, so presence in the request
        # body is what counts, not truthiness — `{"attribute": null}` must
        # remove a wrongly-applied flag, and `None` alone cannot say whether
        # the caller meant "clear it" or "leave it".
        if "attribute" in fields:
            row.attribute = body.attribute
        if "notes" in fields:
            row.notes = (body.notes or None) if body.notes is None else body.notes.strip() or None
        row.source = SOURCE_AGENCY
        await session.flush()
        payload = _row(row)

    log.info("glossary_code_updated", tenant_id=str(tenant_id), code_id=str(code_id))
    return payload


@router.delete("/glossary/{code_id}", status_code=204)
async def delete_code(request: Request, code_id: uuid.UUID) -> None:
    """Remove a code. A starter deleted here does not come back.

    The seed mark is deliberately left behind — it is the record that this
    tenant has already been offered the code and decided against it. Deleting
    the mark alongside the row would make the next glossary read reinstate it.
    """
    _user_id, tenant_id = _require_session(request)

    async with tenant_session(tenant_id) as session:
        result = await session.execute(
            delete(GlossaryCode).where(
                GlossaryCode.id == code_id, GlossaryCode.tenant_id == tenant_id
            )
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="No such code.")

    log.info("glossary_code_deleted", tenant_id=str(tenant_id), code_id=str(code_id))
