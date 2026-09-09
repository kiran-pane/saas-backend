"""Initial multi-tenant schema: tenants, users, RBAC, audit log (partitioned), LLM tables.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-01-01
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- extensions ----
    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")   # gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")     # pgvector

    # ---- tenants ----
    op.execute("""
        CREATE TABLE tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug VARCHAR(63) UNIQUE NOT NULL,
            name VARCHAR(255) NOT NULL,
            plan VARCHAR(50) NOT NULL DEFAULT 'free',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            settings JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # ---- organization units (hierarchical org structure) ----
    op.execute("""
        CREATE TABLE organization_units (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            parent_id UUID REFERENCES organization_units(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            path LTREE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_org_units_path ON organization_units USING GIST (path)")
    op.execute("CREATE INDEX ix_org_units_tenant ON organization_units (tenant_id)")

    # ---- users ----
    op.execute("""
        CREATE TABLE users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email CITEXT NOT NULL,
            hashed_password VARCHAR(255) NOT NULL,
            full_name VARCHAR(255),
            org_unit_id UUID REFERENCES organization_units(id),
            is_active BOOLEAN NOT NULL DEFAULT true,
            is_superadmin BOOLEAN NOT NULL DEFAULT false,
            mfa_secret VARCHAR(255),
            last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, email)
        )
    """)
    op.execute("CREATE INDEX ix_users_tenant ON users (tenant_id)")

    op.execute("""
        CREATE TABLE refresh_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash VARCHAR(255) NOT NULL,
            family_id UUID NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_refresh_tokens_user ON refresh_tokens (user_id)")
    op.execute("CREATE INDEX ix_refresh_tokens_family ON refresh_tokens (family_id)")

    # ---- RBAC ----
    op.execute("""
        CREATE TABLE permissions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(150) UNIQUE NOT NULL,
            resource VARCHAR(100) NOT NULL,
            action VARCHAR(50) NOT NULL,
            description TEXT
        )
    """)

    op.execute("""
        CREATE TABLE roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
            parent_role_id UUID REFERENCES roles(id),
            name VARCHAR(100) NOT NULL,
            is_system BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, name)
        )
    """)
    op.execute("CREATE INDEX ix_roles_parent ON roles (parent_role_id)")

    op.execute("""
        CREATE TABLE role_permissions (
            role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            permission_id UUID NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
            PRIMARY KEY (role_id, permission_id)
        )
    """)

    op.execute("""
        CREATE TABLE user_roles (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            org_unit_id UUID REFERENCES organization_units(id) ON DELETE CASCADE,
            granted_by UUID REFERENCES users(id),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, role_id, org_unit_id)
        )
    """)
    op.execute("CREATE INDEX ix_user_roles_user ON user_roles (user_id)")

    # ---- audit log: partitioned by month, append-only ----
    op.execute("""
        CREATE TABLE audit_logs (
            id BIGSERIAL,
            tenant_id UUID NOT NULL,
            actor_user_id UUID,
            actor_type VARCHAR(20) NOT NULL DEFAULT 'user',
            action VARCHAR(100) NOT NULL,
            resource_type VARCHAR(100),
            resource_id VARCHAR(100),
            request_id VARCHAR(64),
            ip_address INET,
            audit_metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """)
    op.execute("CREATE INDEX ix_audit_tenant_time ON audit_logs (tenant_id, created_at DESC)")
    op.execute("CREATE INDEX ix_audit_resource ON audit_logs (resource_type, resource_id)")

    # Seed current + next month partitions so the app works immediately;
    # app/tasks/audit_tasks.ensure_next_month_partition keeps this rolling.
    op.execute("""
        DO $$
        DECLARE
            m date := date_trunc('month', now());
        BEGIN
            FOR i IN 0..1 LOOP
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
                    'audit_logs_' || to_char(m + (i || ' months')::interval, 'YYYY_MM'),
                    m + (i || ' months')::interval,
                    m + ((i + 1) || ' months')::interval
                );
            END LOOP;
        END $$;
    """)

    # ---- LLM domain tables ----
    op.execute("""
        CREATE TABLE conversations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_conversations_tenant ON conversations (tenant_id)")
    op.execute("CREATE INDEX ix_conversations_user ON conversations (user_id)")

    op.execute("""
        CREATE TABLE documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            uploaded_by UUID NOT NULL REFERENCES users(id),
            filename VARCHAR(500) NOT NULL,
            doc_type VARCHAR(50) NOT NULL DEFAULT 'markdown',
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            source_uri VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_documents_tenant ON documents (tenant_id)")

    op.execute("""
        CREATE TABLE document_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            parent_chunk_id UUID,
            content TEXT NOT NULL,
            chunk_metadata JSONB NOT NULL DEFAULT '{}',
            embedding vector(1536)
        )
    """)
    op.execute("CREATE INDEX ix_chunks_tenant ON document_chunks (tenant_id)")
    op.execute("CREATE INDEX ix_chunks_document ON document_chunks (document_id)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_chunks_fulltext ON document_chunks USING GIN (to_tsvector('english', content))"
    )

    # ---- Row-Level Security: the defense-in-depth guarantee ----
    for table in ("users", "organization_units", "conversations", "documents", "document_chunks"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        """)
    # roles/user_roles are scoped indirectly (tenant_id nullable for system
    # roles) — enforce via application-layer checks in rbac_service rather
    # than a blanket RLS policy, since NULL tenant_id rows must remain
    # visible to every tenant.
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON audit_logs
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
    """)

    # ---- seed baseline permission catalog ----
    op.execute("""
        INSERT INTO permissions (code, resource, action, description) VALUES
        ('rbac.manage', 'rbac', 'manage', 'Create/edit roles, permissions, and role assignments'),
        ('users.read', 'users', 'read', 'View users in the tenant'),
        ('users.write', 'users', 'write', 'Create/edit users in the tenant'),
        ('documents.read', 'documents', 'read', 'View documents'),
        ('documents.write', 'documents', 'write', 'Upload/ingest documents'),
        ('billing.invoice.read', 'billing.invoice', 'read', 'View invoices'),
        ('billing.invoice.delete', 'billing.invoice', 'delete', 'Delete invoices')
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS documents CASCADE")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS user_roles CASCADE")
    op.execute("DROP TABLE IF EXISTS role_permissions CASCADE")
    op.execute("DROP TABLE IF EXISTS roles CASCADE")
    op.execute("DROP TABLE IF EXISTS permissions CASCADE")
    op.execute("DROP TABLE IF EXISTS refresh_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS organization_units CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
