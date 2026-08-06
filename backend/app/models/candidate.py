"""One person an agency places.

Unlike a client, a candidate is never proposed by the pipeline. Email carries
job orders, not CVs — the classifier is binary (`ingest/classify.py:28`) and
drops anything that is not a job order before extraction runs, and attachments
are never downloaded. So every value here has a human author, which is a
stronger provenance claim than any extraction makes and is why none of the
evidence or confidence machinery appears.

Identity is email or phone, either alone. The common case is a recruiter's
older sheet carrying a personal address and the newer one a work address, with
the mobile unchanged; requiring both to agree would duplicate the person every
time. Name is never a key — two different people share a name far more often
than intuition suggests, and merging two real people is worse than a duplicate
somebody can merge later.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from app.models.vector_type import Vector


class Candidate(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidates"

    ACTIVE = "active"
    ARCHIVED = "archived"
    MERGED = "merged"

    # --- Regulatory facts (§15) -------------------------------------------
    #
    # Every vocabulary below is enforced twice — here so a bad value is a 422
    # from Pydantic, and as a database CHECK so the rule holds for a row a
    # migration or script writes directly. Same pattern
    # `20260729_0900_opportunity_vocabularies.py` set.
    #
    # These are FACTS ABOUT A PERSON, recorded because Singapore law requires
    # them on a form. They are not selection criteria, and nothing in this
    # codebase may filter, rank or score on them — see
    # `app/services/sourcing/redact.py` for why that line is drawn where it is.

    FEMALE = "female"
    MALE = "male"
    # Two values because that is the vocabulary of the MOM forms this field
    # exists to fill: a Work Permit application has no third box. NULL is the
    # third state and the default one — "not recorded". Nothing may ever infer
    # this from a name (§15); a name is not evidence of sex, and guessing it
    # would manufacture a regulatory fact nobody stated.
    SEXES = (FEMALE, MALE)

    # Singapore's administrative CMIO categories, as they appear on an NRIC and
    # on every MOM form. Recorded for statutory deductions — CDAC, MBMF, SINDA,
    # ECF are levied by CMIO group — and for those forms. NEVER for selection:
    # race is not a lawful selection criterion in Singapore, and the coded
    # shorthand recruiters use for it ("C/F") is stripped before any model reads
    # a job order (`redact.py`).
    #
    # `others` is the fourth official category, not a catch-all apology; the
    # detail a person actually gives ("Eurasian") goes in `race_detail`, free
    # text, because flattening a Eurasian or Peranakan candidate into a code
    # would be this system deciding what someone is.
    OTHERS = "others"
    RACES = ("chinese", "malay", "indian", OTHERS)

    # The bound the CHECK below enforces, named so the API validator can refuse
    # the value with a readable message instead of letting the database answer
    # with a 500. Not a setting: this is the shape of the column, and a
    # migration that widened it would move this constant with it — unlike
    # `CANDIDATE_MAX_YEARS_EXPERIENCE`, which guards a column that has no
    # constraint to derive anything from.
    EDUCATION_YEARS_MIN = 0
    EDUCATION_YEARS_MAX = 30

    # Named because sourcing has to exclude it by name (a placed person is not
    # available), and a stage spelled out at the query is a stage that can
    # drift from this tuple without anything failing.
    PLACED = "placed"
    STAGES = ("new", "contacted", "submitted", PLACED, "rejected")

    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    # Stored twice on purpose. `phone_raw` is what the recruiter typed and what
    # they recognise; `phone_e164` is the only form two rows can be compared
    # on. Same raw-beside-normalised rule `opportunities` follows.
    phone_raw: Mapped[str | None] = mapped_column(String(64))
    phone_e164: Mapped[str | None] = mapped_column(String(32))

    current_title: Mapped[str | None] = mapped_column(Text)
    current_employer: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    years_experience: Mapped[int | None] = mapped_column(Integer)
    expected_salary: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    # Without the period a monthly and an annual figure average into nonsense
    # — see the comment on `opportunities.salary_period`.
    salary_period: Mapped[str | None] = mapped_column(String(16))
    available_from: Mapped[date | None] = mapped_column(Date)
    notice_period_raw: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String(32))

    # All five nullable, and NULL is the ordinary state: it means "not
    # recorded", never "unknown but guessable". Nothing in this codebase may
    # infer any of them (§15) — not sex from a name, not nationality from a
    # phone prefix, not race from anything at all.
    sex: Mapped[str | None] = mapped_column(String(16))
    race: Mapped[str | None] = mapped_column(String(16))
    # Free text, and only meaningful beside `race = 'others'`. See the RACES
    # comment: the code is what the statutory deduction is levied on, this is
    # what the person actually said they are.
    race_detail: Mapped[str | None] = mapped_column(Text)
    # ISO 3166-1 alpha-2, uppercase. Chosen over a country name because
    # work-pass eligibility turns on it and a name is not a key: "Burma" and
    # "Myanmar" are one country and two strings, and MOM's approved-source-
    # country list for a MDW Work Permit has to be checkable by equality. Two
    # characters is also the form the immigration and MOM systems themselves
    # use, so a value here transcribes onto a form without translation.
    #
    # Deliberately not constrained to the approved-source list. That list is
    # policy and changes; a candidate may hold any nationality, and the column
    # records who somebody is, not whether a particular pass would be granted.
    nationality: Mapped[str | None] = mapped_column(String(2))
    # A date, not an age. Age rots: a row written "23" is wrong within a year
    # and there is nothing in it to say when it was true, so a candidate silently
    # ages out of — or into — a MOM band nobody re-checked. A date of birth is
    # true for ever and every age question can be asked of it at the moment it
    # is asked. Nothing computes or persists an age from this.
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    # Years of formal education completed — a MDW Work Permit requires at least
    # eight. Bounded 0–30 by CHECK: a plausible ceiling for a life spent in
    # education, low enough that a mistyped birth year in the box is refused.
    education_years: Mapped[int | None] = mapped_column(Integer)

    notes: Mapped[str | None] = mapped_column(Text)

    # The R2 object key for the candidate's photo, shown only inside the
    # candidate modal. Nullable because most candidates never get one — a
    # human uploads it deliberately, unlike everything else on this row.
    avatar_key: Mapped[str | None] = mapped_column(Text)
    avatar_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Where the person is in the process, and whether the row is still real.
    # Separate columns because they answer different questions: collapsing them
    # means archiving somebody destroys the fact that they were placed.
    pipeline_stage: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", index=True
    )
    record_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ACTIVE, index=True
    )
    merged_into_candidate_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # Which import wrote or last touched this row, if any. A plain FK, not the
    # composite `(tenant_id, ...)` idiom used elsewhere in this file: a
    # composite FK's bare `ON DELETE SET NULL` nulls every referencing column
    # including `tenant_id`, which is NOT NULL here — the same trap the
    # `merged_into_candidate_id` comment above documents. `CandidateImport` is
    # a record of an event, and deleting that record must never delete the
    # person it created, so this stays SET NULL rather than CASCADE or
    # RESTRICT — the candidate simply becomes one nobody can trace to an
    # import.
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidate_imports.id", ondelete="SET NULL")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # The recruiter this candidate belongs to. NULL means the claimable queue,
    # not "hidden" — the same meaning `opportunities.assigned_user_id` carries.
    #
    # `created_by` stays what it is: an audit column recording who typed the
    # row. Ownership moves; authorship does not, and conflating them would
    # rewrite history every time a candidate changed hands.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidates_tenant_id_id"),
        # CASCADE, and it is the erasure rule rather than a convenience.
        #
        # A row merged into this one is a duplicate record of the same human
        # being, so erasing the person must erase it too — leaving it would
        # keep their name and email in the table a deletion request just
        # cleared.
        #
        # SET NULL cannot work here whichever form it takes. Bare `SET NULL`
        # on a composite key nulls every referencing column including
        # `tenant_id`, which is NOT NULL; the Postgres 15+
        # `SET NULL (merged_into_candidate_id)` form clears only the pointer
        # but then violates `ck_candidates_merged_has_target`, which requires
        # a merged row to name one. Both fail the delete.
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidates_merged_into_same_tenant",
            ondelete="CASCADE",
        ),
        # Composite, so a share can never reach a user in another agency.
        #
        # The column list on SET NULL is not optional: a bare SET NULL on a
        # COMPOSITE key nulls every referencing column including `tenant_id`,
        # which is NOT NULL — so deleting a recruiter would fail outright
        # rather than releasing their candidates to the queue.
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidates_owner_same_tenant",
            ondelete="SET NULL (owner_id)",
        ),
        # Declared here as well as in the migration so autogenerate does not
        # propose dropping them. `merged` is excluded so a merge frees both
        # keys for the surviving row; `archived` stays inside, because an
        # archived person still holds their identity and an import that
        # skipped them would collide on insert instead.
        Index(
            "uq_candidates_tenant_email",
            "tenant_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL AND record_status <> 'merged'"),
        ),
        Index(
            "uq_candidates_tenant_phone",
            "tenant_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL AND record_status <> 'merged'"),
        ),
        # `IS NULL OR ...` on every one of these: NULL is a legal value and
        # means not recorded, so a bare `IN` — which is NULL, not TRUE, for a
        # NULL input — would be satisfied by accident rather than by intent.
        # Spelled out so the reason survives a reader who changes one.
        CheckConstraint(
            "sex IS NULL OR sex IN (" + ",".join(f"'{v}'" for v in SEXES) + ")",
            name="ck_candidates_sex",
        ),
        CheckConstraint(
            "race IS NULL OR race IN (" + ",".join(f"'{v}'" for v in RACES) + ")",
            name="ck_candidates_race",
        ),
        # Uppercase alpha-2 only. A lowercase or three-letter code stored here
        # would compare unequal to the same country written correctly, which
        # for a work-pass eligibility fact is worse than a rejected write.
        CheckConstraint(
            "nationality IS NULL OR nationality ~ '^[A-Z]{2}$'",
            name="ck_candidates_nationality_iso_alpha2",
        ),
        CheckConstraint(
            "education_years IS NULL OR (education_years >= "
            f"{EDUCATION_YEARS_MIN} AND education_years <= {EDUCATION_YEARS_MAX})",
            name="ck_candidates_education_years_range",
        ),
    )


class CandidateSkill(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A skill is a row, not an array element, because it is searched on."""

    __tablename__ = "candidate_skills"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    skill: Mapped[str] = mapped_column(Text, nullable=False)
    skill_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Mirrors `CandidateRole.source`/`.status`: every existing row is
    # human-typed, so the server defaults make the backfill correct with no
    # data migration. Once the CV parser lands (Task 4) a skill can arrive as
    # a proposal instead — `source="cv_upload"`, `status="unconfirmed"` —
    # and wait for a person to confirm or reject it, same lifecycle a role has.
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="human")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_skills_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "skill_normalized",
            name="uq_candidate_skills_once_per_candidate",
        ),
    )


class CandidateLanguage(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A language a candidate speaks, and how well.

    A row rather than an array column on `candidates`, for the same reason
    `CandidateSkill` is one: a language is a *pair* — the language and the
    fluency — so an array would have to be an array of composites, and Postgres
    cannot put a unique constraint on one element of that. Per-candidate
    uniqueness is what stops "English" arriving twice at two different fluencies
    and leaving no answer to which is true. It is also the thing a job order
    will eventually be matched against, and a row is what an index can be built
    on. Deliberately NOT matched on yet: this slice records the fact only.

    `language_normalized` is the comparable form and `language` is what the
    recruiter typed, the same raw-beside-normalised rule the rest of this file
    follows. Normalisation is `normalize_skill`'s — lowercase and collapse
    whitespace, nothing cleverer. A normaliser that aliased would decide that
    "Bahasa" is "Malay" or "Indonesian", and for a MDW placement those are
    different facts about a different person.

    Fluency is a four-value ladder, and it stops where honest observation
    stops. `native` is a fact about upbringing a person states about
    themselves; `fluent`, `conversational` and `basic` are the three
    distinctions a recruiter can actually make from a phone call, which is how
    this value is nearly always obtained. A finer scale (CEFR's six levels)
    would invite a precision nobody measured (§15), and a coarser one would
    lose the conversational/basic line — the one that decides whether a
    helper can be placed with an English-speaking household.
    """

    __tablename__ = "candidate_languages"

    NATIVE = "native"
    FLUENT = "fluent"
    CONVERSATIONAL = "conversational"
    BASIC = "basic"
    FLUENCIES = (NATIVE, FLUENT, CONVERSATIONAL, BASIC)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(Text, nullable=False)
    language_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Nullable: a recruiter who knows somebody speaks Tagalog but has not
    # assessed how well should record the language rather than invent a level.
    fluency: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_languages_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "language_normalized",
            name="uq_candidate_languages_once_per_candidate",
        ),
        CheckConstraint(
            "fluency IS NULL OR fluency IN ('native','fluent','conversational','basic')",
            name="ck_candidate_languages_fluency",
        ),
    )


class CandidateRole(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One job a candidate held.

    The level beneath the flat candidate row: `current_title` says where
    somebody is, this says how they got there, which is the part sourcing can
    reason about.

    Dates carry their own precision because a CV that says "Mar 2019" does not
    say the day. Storing `2019-03-01` and rendering "1 March 2019" would assert
    a fact no source ever stated (§15), so the precision travels with the date
    and the UI renders only what was actually known.

    `source` and `status` have exactly one value each today — a recruiter types
    these rows and they are confirmed on arrival. They exist now because the CV
    parser and the importers land in this same table later, and adding the
    columns then means a migration across every tenant's live data.
    """

    __tablename__ = "candidate_roles"

    HUMAN = "human"
    SOURCES = (HUMAN, "cv_upload", "email_attachment", "import")

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STATUSES = (UNCONFIRMED, CONFIRMED, REJECTED)

    PRECISIONS = ("year", "month", "day")

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    # Raw beside normalised, the same rule `opportunities` and `candidates`
    # follow: the recruiter recognises what they typed, and only the normalised
    # form can be compared against a job order's company name.
    employer: Mapped[str] = mapped_column(Text, nullable=False)
    employer_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Nullable because a CV that gives no dates at all is still worth recording
    # — the employer and title alone are a matchable fact. A NULL `ended_on`
    # with a non-NULL `started_on` means the role is current.
    started_on: Mapped[date | None] = mapped_column(Date)
    started_precision: Mapped[str | None] = mapped_column(String(8))
    ended_on: Mapped[date | None] = mapped_column(Date)
    ended_precision: Mapped[str | None] = mapped_column(String(8))

    employment_type: Mapped[str | None] = mapped_column(String(32))
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default=HUMAN)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CONFIRMED, index=True
    )
    # Set only on a row a model produced, so the evidence behind it can be
    # found. Always NULL while a person is the only writer.
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # Which import wrote or last touched this row, if any. Plain FK, not
    # composite — see the comment on `Candidate.import_id` for why: a
    # composite FK's `SET NULL` would null `tenant_id` too, which is NOT
    # NULL. SET NULL because deleting the import record must not delete the
    # role it created.
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidate_imports.id", ondelete="SET NULL")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_roles_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        # No unique constraint on (candidate, employer, title, started_on).
        # Somebody can genuinely hold the same title at the same employer
        # twice, having left and returned, and refusing the second one would
        # be the system telling a recruiter their own record is wrong.
        CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_candidate_roles_ends_after_start",
        ),
        Index("ix_candidate_roles_candidate_started", "candidate_id", "started_on"),
    )


class CandidateActivity(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """Something a recruiter did towards a candidate, here or outside this app.

    Two outreach paths write here, and they claim different things:

    - The popup path (step 1) renders a message and opens WhatsApp Web; the
      recruiter presses send there, not here. It writes `whatsapp_opened` /
      `OPENED`, which records only that an outreach surface was opened.
    - The WA gateway path (Baileys, plan §7-§8) holds the socket itself, so it
      does observe the handover. It writes `whatsapp_sent` / `SENT`, `FAILED`
      or `UNKNOWN`.

    §15 honesty, the sentence this whole vocabulary turns on:

    `sent` means the gateway's socket accepted the message and WhatsApp
    returned a provider message id — it does not mean delivered, and never
    means read.

    `FAILED` is narrower than "it did not work". It means the gateway
    **explicitly refused**, and `error` then holds the gateway's own message
    verbatim — never paraphrased, never inferred from a status code.

    `UNKNOWN` is the honest answer when we dispatched and never learned the
    outcome: the request went out and the connection timed out or dropped
    before the reply came back, so WhatsApp may well have accepted it. This
    row says exactly that and nothing more. Recording it as `FAILED` would be
    a claim about the world we cannot support, and would invite a recruiter to
    send the same message a second time.

    A row is written only when a dispatch was actually attempted. A refusal
    that happens *before* dispatch — no paired session, no phone number, the
    daily cap already reached — writes nothing at all, because nothing was
    attempted and an activity row is a record of an attempt.

    Delivery receipts are deliberately not ingested in v1, so there is no
    `delivered` or `read` here; adding one requires actually observing
    Baileys' `messages.update`, its own migration, and nothing else may write
    them. `OPENED` keeps its exact original meaning.

    The vocabularies below are enforced twice, the same pattern
    `20260729_0900_opportunity_vocabularies.py` set for
    `opportunities.salary_period`: once here so a bad value is a 422 from
    Pydantic, and once as a database CHECK constraint so the rule holds even
    for a row a future migration or script writes directly.

    Still narrow on purpose. Every value below has code that writes it; none
    is here on speculation, which is why `delivered` and `read` are absent
    despite being the obvious next two.
    """

    __tablename__ = "candidate_activities"

    WHATSAPP_OPENED = "whatsapp_opened"
    WHATSAPP_SENT = "whatsapp_sent"
    ACTIVITY_TYPES = (WHATSAPP_OPENED, WHATSAPP_SENT)

    WHATSAPP = "whatsapp"
    CHANNELS = (WHATSAPP,)

    OPENED = "opened"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
    # P5: written *before* dispatch, inside the same transaction that claims
    # the idempotency key and counts against the daily cap (plan §9 review;
    # see `20260729_2300_wa_send_pending_and_spacing.py`). A reader who sees
    # `pending` should conclude exactly this: we are trying, and we do not
    # yet know. It resolves to `sent`/`failed`/`unknown` once the gateway
    # answers, or to `unknown` via the liveness sweep if it never does — and
    # never to `failed`, because we never observed a refusal (§15).
    PENDING = "pending"
    STATUSES = (OPENED, SENT, FAILED, UNKNOWN, PENDING)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the recruiter actually saw and (presumably) sent, not a re-render of
    # the template — the template can change later and this row must still say
    # what was true the day it was opened.
    message_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # WhatsApp's own id for the message, from the gateway's send call. Null on
    # every `opened` and every `failed` row — the two are mutually exclusive
    # with `error` by construction.
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    # The gateway's own refusal message, verbatim, for a `failed` row. Only
    # ever a message the gateway itself produced — never this module's guess
    # at one, and never a string that could carry the gateway URL or the
    # shared secret into a column the browser reads back.
    error: Mapped[str | None] = mapped_column(Text)
    # The caller's idempotency key: a retried click carries the same one, and
    # gets the original row back rather than sending twice. Unique per tenant
    # (see the migration), not global.
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_activities_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        Index(
            "uq_candidate_activities_tenant_client_request",
            "tenant_id",
            "client_request_id",
            unique=True,
        ),
    )


class CandidateFieldOverride(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A field a person edited, which no import may overwrite.

    The shape is borrowed from `opportunity_field_overrides`, but that table is
    a model and nothing else — nothing reads or writes it. There is no working
    implementation to copy, so this is new machinery. The justification differs
    too: there it guards against an AI re-extraction clobbering a human, here
    the only thing that would overwrite is a later import of a stale sheet.
    """

    __tablename__ = "candidate_field_overrides"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    human_value: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Whose reading this is. NULL is a distinct, permanent tier meaning
    # "agency-wide import protection" — the meaning every row written before
    # candidates had owners carries, and the meaning a shared base fact keeps.
    #
    # `changed_by` is not this column and cannot be: it is a nullable SET NULL
    # audit trail, so it empties when the account is deleted, and an identity
    # key that vanishes is not an identity key.
    user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_overrides_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "user_id",
            "field_name",
            name="uq_candidate_overrides_one_per_field_per_user",
        ),
        # A NULL does not collide with another NULL in a Postgres UNIQUE
        # constraint, so the constraint above does not bound the tenant-wide
        # tier. This does.
        #
        # Postgres 15+ offers `UNIQUE NULLS NOT DISTINCT` as an alternative.
        # It is not used here: the partial index states the tenant-wide tier
        # explicitly, and the two rules — "one per user per field" and "one
        # agency-wide per field" — read as two rules because they are two.
        Index(
            "uq_candidate_overrides_one_tenant_wide_per_field",
            "tenant_id",
            "candidate_id",
            "field_name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        # CASCADE, not SET NULL: a departed recruiter's private opinion must
        # not silently become agency-wide import protection, which is exactly
        # what SET NULL would do here.
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_overrides_user_same_tenant",
            ondelete="CASCADE",
        ),
    )


class CandidateDocument(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A CV a candidate came with.

    The extracted text is stored (`text_key`, `text_chars`) rather than
    re-derived on demand, because an evidence span (`ExtractionEvidence.
    start_char`/`end_char`) is an offset into it — the exact bytes the
    extraction saw have to still exist for that offset to mean anything
    later. The original file (`object_key`) is kept separately for that same
    reason: re-extracting text from a re-parsed PDF is not guaranteed to
    reproduce the same offsets a stored extraction already points into.

    `unreadable` and `failed` are two different terminal states, not one,
    because they answer different questions. A corrupt or scanned-image PDF
    is `unreadable` — asking again gets the same answer forever, so the UI
    should stop offering retry. A timeout or a transient parser crash is
    `failed` — a bad minute, worth trying again without anyone editing
    anything.
    """

    __tablename__ = "candidate_documents"

    # A CV uploaded with no candidate named starts here: the ingest job has not
    # yet read its identity and resolved it to a candidate. Distinct from
    # `pending` so `rescan_stuck` can route a stranded row to the right job —
    # `ingest_candidate_cv` for this state, `parse_candidate_cv` for `pending`
    # and `parsing` — and so the per-candidate upload path (which has always
    # named its candidate) never enters it by accident.
    INGEST_PENDING = "ingest_pending"
    INGESTING = "ingesting"
    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    # Text came out of the file, the model read it, and it described no role
    # and named no skill. Terminal like `parsed`, and separate from it because
    # a blank candidate panel otherwise looks the same as a full one that has
    # not loaded — see the `cv_parse_outcome` migration.
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    FAILED = "failed"
    # Identity was resolved but the resolved candidate belongs to a colleague
    # this recruiter cannot see (or two keys pointed at two people). Terminal,
    # because neither is a transient failure: a person must look at it. The
    # roles/skills parse is not run while the document's candidate is in
    # dispute, so nothing the parse found gets attached to the wrong person.
    NEEDS_REVIEW = "needs_review"
    PARSE_STATES = (
        INGEST_PENDING, INGESTING, PENDING, PARSING, PARSED,
        EMPTY, UNREADABLE, FAILED, NEEDS_REVIEW,
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # The R2 object key for the uploaded file as received.
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The R2 object key for the extracted plain text, set once parsing
    # succeeds. Nullable because most of a document's life is spent before
    # that — `pending`, `parsing`, or a terminal failure that never produced
    # text.
    text_key: Mapped[str | None] = mapped_column(Text)
    text_chars: Mapped[int | None] = mapped_column(Integer)

    parse_state: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    parse_error: Mapped[str | None] = mapped_column(Text)

    # What the extractor read but would not publish. A role or skill whose
    # quotation is not on the page is discarded (`cv.extract`), and without a
    # count beside the document a recruiter has no way to tell "the CV listed
    # three jobs" from "two of the five were thrown away". `dropped_reason` is
    # the sentence shown next to it; both stay set on a `parsed` document, so
    # this is a note about a success, not an error.
    dropped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dropped_reason: Mapped[str | None] = mapped_column(Text)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # Where this document entered the platform. `upload` is the per-candidate
    # drop-zone; `ingest` is a CV uploaded with no candidate named, resolved by
    # reading its identity. Cloud sources (`onedrive`, `sharepoint`, `gdrive`)
    # arrive later with the storage-connections feature and share this column
    # so a document's provenance has one home rather than two.
    UPLOAD = "upload"
    INGEST = "ingest"
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default=UPLOAD)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_documents_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "parse_state IN ('ingest_pending','ingesting','pending','parsing',"
            "'parsed','empty','unreadable','failed','needs_review')",
            name="ck_candidate_documents_parse_state",
        ),
    )


class CandidateEmbedding(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A candidate's CV as a vector, for semantic matching against a job order.

    A derivative artefact of the parsed CV text stored at
    `CandidateDocument.text_key`: the vector is a recomputeable function of
    text the system already holds, kept in its own table so re-embedding under
    a different model is an upsert here rather than a churn of the candidate
    row's `updated_at`. The `(tenant_id, candidate_id, model)` unique key is
    what makes "one vector per candidate per model" the enforced shape.

    Privacy: the text sent to the embedding provider is CV text that already
    leaves the system for LLM explanations (Cerebras). Embeddings add no new
    data boundary; they add a second provider, gated by `EMBEDDING_API_KEY`.
    """

    __tablename__ = "candidate_embeddings"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    # Which embedding produced the row. A model swap is a new row, not an
    # overwrite — the old one stays until a backfill retires it, so a run can
    # fall back if the new model misbehaves.
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_embeddings_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "model",
            name="uq_candidate_embeddings_once_per_model",
        ),
    )


class CandidateImport(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A spreadsheet a recruiter uploaded to bulk-load or bulk-update candidates.

    Modelled on `CandidateDocument` — same mixins, same object-key-plus-
    metadata shape — but the state machine is an import's, not a parse's:
    `pending` while the file sits in R2 waiting for the job, `parsing` while
    rows are read and matched, and one of three terminals. `done` and `failed`
    mirror `CandidateDocument.PARSED`/`.FAILED`; `undone` is new — the row
    stays after undo runs (Task 6) rather than being deleted, because deleting
    it would erase the counts below and leave no evidence the import, or its
    reversal, ever happened.

    The four `*_created`/`*_updated` counters and `rows_failed` exist so the
    upload result and the eventual undo confirmation can both be answered from
    this one row without re-deriving them from `CandidateImportChange`, which
    a large import could make thousands of rows long.
    """

    __tablename__ = "candidate_imports"

    PENDING = "pending"
    PARSING = "parsing"
    DONE = "done"
    FAILED = "failed"
    UNDONE = "undone"
    IMPORT_STATES = (PENDING, PARSING, DONE, FAILED, UNDONE)

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # The R2 object key for the uploaded spreadsheet as received.
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The R2 object key for a per-row error report, set only if some rows
    # failed to parse or match. Nullable because most imports need none.
    error_report_key: Mapped[str | None] = mapped_column(Text)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)

    candidates_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roles_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roles_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Rows matched to somebody a colleague owns, which this import was not
    # allowed to edit. Reported rather than dropped silently: an import that
    # applied fewer rows than the file held looks like a bug otherwise.
    held_by_colleagues: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # How many times a worker has picked this import up. Counted at pickup
    # rather than at completion, because the run this bounds is the one that
    # never completes: a file that crashes `apply_import` leaves the row
    # non-terminal, `rescan_stuck` re-enqueues it, and without a count that
    # survives the crash the pair loop for ever. Past
    # `IMPORT_MAX_ATTEMPTS` the job parks the row in `failed` instead.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidate_imports_tenant_id_id"),
        CheckConstraint(
            "state IN ('pending','parsing','done','failed','undone')",
            name="ck_candidate_imports_state",
        ),
    )


class CandidateImportChange(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One field an import wrote, kept so the import can be walked back.

    Undo (Task 6) restores a field only if its current value still equals
    what the import wrote — a recruiter who retyped the field afterwards owns
    it now, and undo must not clobber that edit. Evaluating that rule needs
    both sides of the comparison: `previous_value` alone tells undo what to
    restore *to*, but not whether restoring is still safe, because there is
    nothing to check the current value against. `new_value` is that other
    half. Dropping it would make undo either silently wrong (always restore)
    or silently inert (never restore) — both look complete and are not.

    `previous_value` is nullable and stays NULL on a `created` row: there is
    no "before" for a field that did not exist, and undo of a `created` row
    deletes the entity rather than restoring anything.

    `entity_type`/`entity_id` point at the changed `Candidate` or
    `CandidateRole` row rather than a typed FK to either, because one column
    covering both saves a UNION when Task 6 walks the whole import back in id
    order; the tenant-scoped uniqueness on both target tables is what keeps
    a stray id from resolving into another tenant's row.
    """

    __tablename__ = "candidate_import_changes"

    CANDIDATE = "candidate"
    ROLE = "role"
    ENTITY_TYPES = (CANDIDATE, ROLE)

    CREATED = "created"
    UPDATED = "updated"
    ACTIONS = (CREATED, UPDATED)

    import_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "import_id"],
            ["candidate_imports.tenant_id", "candidate_imports.id"],
            name="fk_candidate_import_changes_import_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "entity_type IN ('candidate','role')",
            name="ck_candidate_import_changes_entity_type",
        ),
        CheckConstraint(
            "action IN ('created','updated')",
            name="ck_candidate_import_changes_action",
        ),
    )
