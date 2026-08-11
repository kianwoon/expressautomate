"""body_hash column: skip duplicate classification and replay

The same job order arrives more than once — a client sends it to two
recruiters at one agency, a reply chain re-fetches an earlier message, delta
sync re-delivers a message Graph moved between folders. Each copy pays the
gate, and the replay sweep pays extraction again on every prompt bump even
when the body never changed.

This migration adds `email_messages.body_hash`: the sha256 of the exact text
the gate and extractor see (`to_text(html, subject, sender)`), computed once
at fetch time and stored next to the body keys. The classify jobs group by it
— a second email whose body hash already has a verdict is answered from that
verdict without a model call — and the replay sweep can skip emails whose
body hash already has a current-prompt extraction.

The hash is deliberately computed over the *processed text*, not the raw
HTML: two HTML renderings that flatten to the same text (whitespace noise,
tracking pixel, font markup) are the same job order to the models that read
the text, so they should pay once.

Revision ID: c1v2i0000001
Revises: c1v2h0000001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2i0000001"
down_revision: str | None = "c1v2h0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("body_hash", sa.String(length=64), nullable=True))
    op.create_index(
        op.f("ix_email_messages_body_hash"),
        "email_messages",
        ["body_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_messages_body_hash"), table_name="email_messages")
    op.drop_column("email_messages", "body_hash")
