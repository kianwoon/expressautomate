"""candidate activities

Step 1 of WhatsApp outreach: the platform opens WhatsApp Web with a rendered
message, but the recruiter presses send there. This table records that an
outreach surface was opened — never that a message was sent, because nothing
in this system observes a send (§15). `status` is CHECK-constrained to
`'opened'` only, so a `'sent'` value is impossible to write even by mistake or
by a later script, not merely undocumented.

RLS policy created in the same revision as the table, same reasoning as
`20260728_1800_candidate_roles.py`: `verify_rls_enforced()` refuses to boot on
any readable table without a forced policy.

Revision ID: 5c2d8e14f6a1
Revises: 3f9a6c21e8b4
Create Date: 2026-07-29 10:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "5c2d8e14f6a1"
# Rebased onto main's head rather than the revision this branch was cut from.
# Left pointing at the old parent it made a second alembic head, and
# `upgrade head` refuses to choose between two — the deploy fails before it
# reaches the table. This branch adds a table nothing else references, so
# following main's chain costs nothing and racing it costs the release.
down_revision: str | None = "f4b8c1e7d290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.tenant_id"

# Mirrors `CandidateActivity` in app/models/candidate.py — written out rather
# than imported, same reasoning as `_PERIODS` in the opportunity-vocabularies
# migration: this revision must keep meaning what it meant the day it ran.
_ACTIVITY_TYPES = ("whatsapp_opened",)
_CHANNELS = ("whatsapp",)
_STATUSES = ("opened",)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "candidate_activities",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("activity_type", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("message_text", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_activities_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            f"activity_type IN ({_in_list(_ACTIVITY_TYPES)})",
            name="ck_candidate_activities_type_known",
        ),
        sa.CheckConstraint(
            f"channel IN ({_in_list(_CHANNELS)})",
            name="ck_candidate_activities_channel_known",
        ),
        # The rule that matters most in this table: `sent` can never land here,
        # because nothing in the system observes a send (§15).
        sa.CheckConstraint(
            f"status IN ({_in_list(_STATUSES)})",
            name="ck_candidate_activities_status_known",
        ),
    )
    op.create_index("ix_candidate_activities_tenant_id", "candidate_activities", ["tenant_id"])
    op.create_index("ix_candidate_activities_candidate_id", "candidate_activities", ["candidate_id"])
    op.create_index(
        "ix_candidate_activities_candidate_created",
        "candidate_activities",
        ["candidate_id", "created_at"],
    )

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_activities TO "{settings.DATABASE_APP_ROLE}"')

    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
    op.execute("ALTER TABLE candidate_activities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_activities FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_activities")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_activities
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_activities")
    op.drop_table("candidate_activities")
