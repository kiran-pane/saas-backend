"""Add storage_provider to documents (supports live provider migration —
some documents on the old provider, some on the new one, during a
cutover) and a payment_webhook_events table stub referenced by the
payments design doc is intentionally NOT included here (out of scope for
this migration; add separately when payments land).

Revision ID: 0003_storage_layer
Revises: 0002_google_oauth
Create Date: 2026-01-03
"""
from alembic import op

revision = "0003_storage_layer"
down_revision = "0002_google_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents ADD COLUMN storage_provider VARCHAR(20) DEFAULT 'local'"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN size_bytes BIGINT"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN content_type VARCHAR(150)"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN scan_signature VARCHAR(255)"
    )
    # documents.status already exists as VARCHAR(20) with no CHECK
    # constraint (application-enforced), so no migration is needed to
    # widen the set of valid values (pending_upload | scanning | ready |
    # rejected_virus | rejected_invalid_type | failed | archived) —
    # enforced in app/models/llm.py / app/tasks/storage_tasks.py.


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS scan_signature")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS content_type")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS size_bytes")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS storage_provider")
