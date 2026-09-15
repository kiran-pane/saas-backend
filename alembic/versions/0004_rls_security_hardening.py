"""Security hardening pass (see SECURITY_COMPLIANCE_AUDIT.md):
1. RLS on roles/user_roles — previously the ONLY tenant tables without a
   database-level backstop, relying entirely on application-layer
   filtering. System roles (tenant_id IS NULL) must remain visible across
   tenants, so the policy allows NULL through in addition to the matching
   tenant_id.
2. Audit log partition expiry support — a retention_months column concept
   is enforced at the application/Celery-beat layer (see
   app/tasks/audit_tasks.py::drop_expired_audit_partitions), no schema
   change needed for that part; included here as a single "security
   hardening" migration for traceability.

Revision ID: 0004_rls_security_hardening
Revises: 0003_storage_layer
Create Date: 2026-01-04
"""
from alembic import op

revision = "0004_rls_security_hardening"
down_revision = "0003_storage_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE on RLS + table ownership: Postgres RLS policies are bypassed by
    # a table's owning role by default (and by superusers), unless
    # `FORCE ROW LEVEL SECURITY` is also set. If the app's DB user
    # (saas_app) is the table owner — the common case when it also ran
    # the migrations — add `ALTER TABLE <table> FORCE ROW LEVEL SECURITY`
    # for every RLS-protected table (this one and the ones from 0001) as
    # a follow-up hardening pass, ideally paired with running the app
    # under a DIFFERENT, non-owning DB role than the one used for
    # migrations. Not applied here to avoid silently changing behavior
    # for the tables from 0001 in a migration whose stated scope is
    # roles/user_roles — track as a separate, deliberate change.

    # roles: tenant_id IS NULL rows are global/system roles and must
    # remain visible to every tenant (they're the templates "Owner"/
    # "Member" are cloned from conceptually, and any future truly-global
    # role) — so the policy is an OR, not a plain equality check.
    op.execute("ALTER TABLE roles ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON roles
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.current_tenant', true)::uuid
        )
    """)

    # user_roles has no tenant_id column of its own — it's scoped
    # transitively via the role it references. RLS can't directly express
    # "join to roles and check tenant_id" in a USING clause efficiently
    # without a subquery, but a subquery-based policy is still correct
    # and Postgres can use the roles.tenant_id index inside it.
    op.execute("ALTER TABLE user_roles ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON user_roles
        USING (
            role_id IN (
                SELECT id FROM roles
                WHERE tenant_id IS NULL
                   OR tenant_id = current_setting('app.current_tenant', true)::uuid
            )
        )
    """)

    # role_permissions and permissions are global reference data (the
    # permission catalog itself, and the role->permission link table) —
    # deliberately NOT tenant-scoped, same rationale as tenant_id IS NULL
    # system roles. No RLS policy added here; this is an explicit design
    # choice, not an oversight (see the comment in app/models/rbac.py).


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON user_roles")
    op.execute("ALTER TABLE user_roles DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON roles")
    op.execute("ALTER TABLE roles DISABLE ROW LEVEL SECURITY")
