"""row level security

Enforces tenant isolation in the database (plan §18, §30).

Creates a runtime application role that lacks BYPASSRLS, then puts every
tenant-scoped table behind a policy keyed on the `app.tenant_id` connection
setting. FORCE is used so the table owner is subject to the policy too.

Revision ID: 9c1f4a2b7e30
Revises: 5602790edcf3
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "9c1f4a2b7e30"
down_revision: str | None = "5602790edcf3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column compared against app.tenant_id)
PROTECTED: list[tuple[str, str]] = [
    ("tenants", "id"),
    ("users", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    role = settings.DATABASE_APP_ROLE
    password = settings.DATABASE_APP_PASSWORD
    if not password:
        raise RuntimeError(
            "DATABASE_APP_PASSWORD is not set — refusing to create the application role "
            "without a password."
        )

    quoted_role = f'"{role}"'
    conn = op.get_bind()

    # CREATE/ALTER ROLE ... PASSWORD takes a literal, not a bind parameter, so
    # the value is interpolated. Postgres itself does the quoting via
    # quote_literal — never string formatting.
    literal_pw = conn.execute(
        sa.text("SELECT quote_literal(:pw)"), {"pw": password}
    ).scalar_one()

    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar_one_or_none()

    # NOBYPASSRLS is the whole point: this role must never see past a policy.
    verb = "ALTER" if exists else "CREATE"
    op.execute(f"{verb} ROLE {quoted_role} WITH LOGIN PASSWORD {literal_pw} NOBYPASSRLS")

    # Database name comes from the connection, never a literal — CI, local and
    # production all run this migration against differently-named databases.
    db_name = conn.execute(sa.text("SELECT quote_ident(current_database())")).scalar_one()
    op.execute(f"GRANT CONNECT ON DATABASE {db_name} TO {quoted_role}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted_role}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted_role}")
    # Tables created by later migrations inherit these grants automatically.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted_role}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {quoted_role}"
    )

    # The app role must NOT be able to read alembic_version; migrations are the
    # admin role's job.
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {quoted_role}")

    for table, column in PROTECTED:
        # NULLIF is load-bearing. Once set_config has run on a connection the
        # setting stays *defined* for that backend, and a later transaction
        # that does not scope itself reads back '' rather than NULL — casting
        # that to uuid raises instead of matching nothing. NULLIF turns the
        # empty case back into NULL, so the predicate is NULL, no row matches,
        # and an unscoped session quietly sees zero rows.
        predicate = f"{column} = nullif(current_setting('{SETTING}', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )


def downgrade() -> None:
    role = settings.DATABASE_APP_ROLE
    quoted_role = f'"{role}"'
    conn = op.get_bind()
    db_name = conn.execute(sa.text("SELECT quote_ident(current_database())")).scalar_one()

    for table, _ in PROTECTED:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {quoted_role}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {quoted_role}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {quoted_role}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted_role}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {quoted_role}")
    op.execute(f"REVOKE ALL ON DATABASE expressautomate FROM {quoted_role}")
    # The role itself is left in place; dropping it would fail while any
    # dependent object or open session remains.
