"""Per-agency shorthand, and the single definition of "the same code".

Clients write job orders in shorthand — `C/F`, `o/o`, `C/O` — and the same
three characters mean different things to different agencies. `C/O` is
"Chinese, gender open" to one and "care of" to the next, so this is per-tenant
data and never a global constant: a shared table would decide one agency's
hiring requirement using another agency's dictionary.

Two rules hold this together.

**One normalised form is unique per tenant.** Detection matches the email text
case- and punctuation-insensitively, so `C/F`, `c/f` and `C / F` all hit the
same definition. If all three could also be stored as separate rows, the
agency would have three meanings competing for one match and no way to see
which won. The uniqueness therefore sits on the normalised form, not on what
was typed, and `code` is kept alongside only so the UI can show the operator
their own spelling back.

**`attribute` is stated, not inferred.** The product decodes the shorthand
*and* flags that it touches a protected characteristic. Guessing which codes
are sensitive from their meaning text would make that flag a model output,
quietly wrong on the codes that matter most; the operator names it, and this
column is what the flag reads.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

# The protected characteristics a code may reference. allow-hardcode: this is
# the schema's own enumeration — the CHECK constraint in the migration lists
# exactly these values, and the two must agree or an insert that passes here
# fails in the database. Making it configurable would let a deployment offer
# the UI a value the column rejects.
PROTECTED_ATTRIBUTES: tuple[str, ...] = (
    "race",
    "nationality",
    "gender",
    "age",
    "religion",
    "marital_status",
)

# Who wrote the meaning. A starter row is ours until an operator confirms it,
# which matters because a wrong default silently mis-reads a client's actual
# requirement — the UI can only mark that uncertainty if the column records it.
SOURCE_STARTER = "starter"
SOURCE_AGENCY = "agency"
SOURCES: tuple[str, ...] = (SOURCE_STARTER, SOURCE_AGENCY)

def normalise(code: str) -> str:
    """The form two codes must share to be "the same code".

    Case-folded, then stripped of everything that is not a letter or a digit.
    `C/F`, `c/f`, `C / F` and `C.F.` all become `cf`.

    "Letter or digit" is **Unicode-wide**, via `str.isalnum`, and that is a
    decision rather than a default. An ASCII-only class discards every other
    script instead of keeping it, which is not a narrower rule — it is a
    *wrong* one: `Bé/F` and `B/F` would both fold to `bf`, so an agency adding
    the second would be told it collides with the first, and a client writing
    a Chinese-character code would find every one of them normalising to the
    same empty-ish key. Silently deleting a character an operator typed makes
    two distinct codes indistinguishable, which is precisely the failure this
    function exists to prevent. Separators and punctuation are the only things
    that should vanish here, because they are the only thing clients vary.

    This is deliberately the *only* definition in the codebase — the detection
    pass imports it rather than reimplementing it. Two implementations that
    drift by one character produce the worst possible failure: a code the
    agency can see in their glossary and that the matcher never fires on, with
    nothing on screen to explain why.

    `casefold` rather than `lower`: it is the comparison-grade fold, and the
    unique index this feeds must not treat two spellings of one word as two
    codes.
    """
    # A comprehension rather than a regex: `str.isalnum` is the same
    # letter-or-digit predicate the Unicode database defines, and no character
    # class written by hand stays equal to it across releases.
    return "".join(ch for ch in code.casefold() if ch.isalnum())


class GlossaryCode(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One agency's definition of one piece of shorthand."""

    __tablename__ = "glossary_codes"
    __table_args__ = (
        # The uniqueness that makes a match unambiguous. On (tenant_id, ...)
        # and not on the normalised form alone: agencies legitimately disagree.
        UniqueConstraint("tenant_id", "code_normalised", name="uq_glossary_codes_tenant_code"),
        CheckConstraint(
            "attribute IS NULL OR attribute IN "
            "('race','nationality','gender','age','religion','marital_status')",
            name="ck_glossary_codes_attribute",
        ),
        CheckConstraint("source IN ('starter','agency')", name="ck_glossary_codes_source"),
        CheckConstraint("code_normalised <> ''", name="ck_glossary_codes_normalised_not_empty"),
    )

    # What the operator typed, preserved for display only.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    # What matching and uniqueness actually use.
    code_normalised: Mapped[str] = mapped_column(String(64), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    attribute: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=SOURCE_AGENCY)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class GlossarySeedMark(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A ledger of every starter code this tenant has *ever* been offered.

    Seeding without this ledger is not idempotent in the way that matters. An
    "insert if the code is absent" seed cannot tell a code the agency deleted
    on purpose from one they have not been given yet, so the next seed pass
    resurrects the deletion — and an agency that removed a wrong default would
    watch it come back and reasonably conclude the delete button is broken.

    A row here means "we have offered this code, and the decision about it is
    now theirs". It is written when the code is seeded and never removed, so a
    deleted starter stays deleted and an edited one is never overwritten, while
    a starter code added to the list in a later release still reaches every
    existing tenant exactly once.
    """

    __tablename__ = "glossary_seed_marks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code_normalised", name="uq_glossary_seed_marks_tenant_code"),
    )

    code_normalised: Mapped[str] = mapped_column(String(64), nullable=False)
