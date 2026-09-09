"""Add Google OAuth support: nullable password, oauth_provider/oauth_sub on users.

Revision ID: 0002_google_oauth
Revises: 0001_initial_schema
Create Date: 2026-01-02
"""
from alembic import op

revision = "0002_google_oauth"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL")
    op.execute("ALTER TABLE users ADD COLUMN oauth_provider VARCHAR(30)")
    op.execute("ALTER TABLE users ADD COLUMN oauth_sub VARCHAR(255)")

    # A user can only have one account per (tenant, provider, sub) —
    # prevents duplicate account creation on repeated Google sign-ins.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_users_tenant_oauth
        ON users (tenant_id, oauth_provider, oauth_sub)
        WHERE oauth_provider IS NOT NULL
        """
    )

    # Guard rail: every user must have *some* way to authenticate —
    # either a password or a linked OAuth identity, never neither.
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_has_auth_method
        CHECK (hashed_password IS NOT NULL OR oauth_provider IS NOT NULL)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_has_auth_method")
    op.execute("DROP INDEX IF EXISTS uq_users_tenant_oauth")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS oauth_sub")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS oauth_provider")
    op.execute("ALTER TABLE users ALTER COLUMN hashed_password SET NOT NULL")
