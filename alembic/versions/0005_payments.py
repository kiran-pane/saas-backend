"""Payments: plans (global catalog), subscriptions, payments, and a
webhook-event dedup table. Amounts stored in integer cents to avoid
floating-point rounding in billing math.

Revision ID: 0005_payments
Revises: 0004_rls_roles_and_security_hardening
Create Date: 2026-01-05
"""
from alembic import op

revision = "0005_payments"
down_revision = "0004_rls_roles_and_security_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(50) UNIQUE NOT NULL,
            name VARCHAR(150) NOT NULL,
            amount_cents INTEGER NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'usd',
            interval VARCHAR(20) NOT NULL DEFAULT 'month',
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE subscriptions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            plan_id UUID NOT NULL REFERENCES plans(id),
            provider VARCHAR(20) NOT NULL,
            provider_subscription_id VARCHAR(255),
            provider_customer_id VARCHAR(255),
            status VARCHAR(30) NOT NULL DEFAULT 'incomplete',
            current_period_end TIMESTAMPTZ,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_subscriptions_tenant ON subscriptions (tenant_id)")
    op.execute("CREATE INDEX ix_subscriptions_provider_sub_id ON subscriptions (provider, provider_subscription_id)")

    op.execute("""
        CREATE TABLE payments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            subscription_id UUID REFERENCES subscriptions(id),
            plan_id UUID REFERENCES plans(id),
            provider VARCHAR(20) NOT NULL,
            provider_payment_id VARCHAR(255) NOT NULL,
            amount_cents INTEGER NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'usd',
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            failure_reason VARCHAR(500),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_payments_tenant ON payments (tenant_id)")
    op.execute("CREATE UNIQUE INDEX uq_payments_provider_payment_id ON payments (provider, provider_payment_id)")

    op.execute("""
        CREATE TABLE payment_webhook_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider VARCHAR(20) NOT NULL,
            provider_event_id VARCHAR(255) NOT NULL,
            event_type VARCHAR(100) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}',
            processed BOOLEAN NOT NULL DEFAULT false,
            processing_error VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (provider, provider_event_id)
        )
    """)

    # RLS on the two tenant-scoped tables — same pattern as every other
    # tenant table in this schema.
    for table in ("subscriptions", "payments"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        """)

    # Seed a starter plan catalog — adjust pricing/codes to your actual
    # product before going live; this exists so the payments flow is
    # testable immediately after migrating.
    op.execute("""
        INSERT INTO plans (code, name, amount_cents, currency, interval) VALUES
        ('free', 'Free', 0, 'usd', 'month'),
        ('pro_monthly', 'Pro (Monthly)', 4900, 'usd', 'month'),
        ('pro_yearly', 'Pro (Yearly)', 49000, 'usd', 'year'),
        ('enterprise_monthly', 'Enterprise (Monthly)', 49900, 'usd', 'month')
    """)

    # New RBAC permissions for billing management.
    op.execute("""
        INSERT INTO permissions (code, resource, action, description) VALUES
        ('billing.subscription.read', 'billing.subscription', 'read', 'View subscription and payment history'),
        ('billing.subscription.manage', 'billing.subscription', 'manage', 'Change plan, cancel subscription')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS payment_webhook_events CASCADE")
    op.execute("DROP TABLE IF EXISTS payments CASCADE")
    op.execute("DROP TABLE IF EXISTS subscriptions CASCADE")
    op.execute("DROP TABLE IF EXISTS plans CASCADE")
