"""A deliberately NON-RLS-protected lookup table mapping
(provider, provider_subscription_id) -> tenant_id.

Why this exists: subscriptions/payments ARE RLS-protected (correctly —
they're tenant data). But webhook processing runs with no tenant context
(a webhook carries no X-Tenant-Slug header), so before the session's
`app.current_tenant` variable is set, an RLS-protected SELECT against
`subscriptions` to find "which tenant does this provider_subscription_id
belong to" would match ZERO rows — RLS can't be used to resolve the very
tenant_id needed to satisfy RLS. This index table breaks that circularity:
it's populated once, out-of-band of RLS, and used purely for tenant
resolution before the session variable is set for the real (RLS-protected)
read/write against `subscriptions`/`payments`.

Revision ID: 0006_subscription__index
Revises: 0005_payments
Create Date: 2026-01-06
"""
from alembic import op

revision = "0006_subscription__index"
down_revision = "0005_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE subscription_provider_index (
            provider VARCHAR(20) NOT NULL,
            provider_subscription_id VARCHAR(255) NOT NULL,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, provider_subscription_id)
        )
    """)
    # Deliberately NO RLS on this table — see module docstring. It holds
    # no data beyond an id-to-tenant mapping (no amounts, no PII), and
    # existing purely to be queryable BEFORE tenant context is known.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS subscription_provider_index")
