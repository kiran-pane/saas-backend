"""Knowledge Base / RAG / Chat: knowledge_bases, access grants, projects,
messages, conversation KB toggle, user chat preferences, user memory
facts. Extends documents/document_chunks/conversations with the columns
described in CHAT_RAG_AGENT_ARCHITECTURE_V2.md.

Revision ID: 0008_knowledge_base_rag
Revises: 0007_media_streaming
Create Date: 2026-01-08
"""
from alembic import op

revision = "0008_knowledge_base_rag"
down_revision = "0007_media_streaming"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- knowledge_bases ----
    op.execute("""
        CREATE TABLE knowledge_bases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            name VARCHAR(255) NOT NULL,
            description VARCHAR(1000),
            access_mode VARCHAR(20) NOT NULL DEFAULT 'tenant_wide',
            embedding_model VARCHAR(100) NOT NULL,
            embedding_dimension INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            document_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            total_size_bytes BIGINT NOT NULL DEFAULT 0,
            embedding_model_deprecated_at TIMESTAMPTZ,
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_kb_tenant ON knowledge_bases (tenant_id)")

    op.execute("""
        CREATE TABLE knowledge_base_access_grants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            grantee_type VARCHAR(10) NOT NULL,
            grantee_id UUID NOT NULL,
            granted_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_kb_grants_kb ON knowledge_base_access_grants (knowledge_base_id)")
    op.execute("CREATE INDEX ix_kb_grants_grantee ON knowledge_base_access_grants (grantee_type, grantee_id)")

    # ---- projects ----
    op.execute("""
        CREATE TABLE projects (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            name VARCHAR(255) NOT NULL,
            system_instructions TEXT,
            default_kb_ids JSONB NOT NULL DEFAULT '[]',
            created_by UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_projects_tenant ON projects (tenant_id)")

    # ---- conversations: extend ----
    op.execute("ALTER TABLE conversations ADD COLUMN project_id UUID REFERENCES projects(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE conversations ADD COLUMN title_generation_status VARCHAR(20) NOT NULL DEFAULT 'pending'")
    op.execute("ALTER TABLE conversations ADD COLUMN forked_from_conversation_id UUID")
    op.execute("ALTER TABLE conversations ADD COLUMN forked_at_message_id UUID")
    op.execute("CREATE INDEX ix_conversations_project ON conversations (project_id)")

    # ---- messages ----
    op.execute("""
        CREATE TABLE messages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            token_count INTEGER,
            used_kb_ids JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_messages_tenant ON messages (tenant_id)")
    op.execute("CREATE INDEX ix_messages_conversation ON messages (conversation_id, created_at)")

    # ---- conversation KB toggle ----
    op.execute("""
        CREATE TABLE conversation_knowledge_bases (
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            PRIMARY KEY (conversation_id, knowledge_base_id)
        )
    """)

    # ---- documents/document_chunks: extend ----
    op.execute("ALTER TABLE documents ADD COLUMN knowledge_base_id UUID REFERENCES knowledge_bases(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE documents ADD COLUMN superseded_by_id UUID")
    op.execute("CREATE INDEX ix_documents_kb ON documents (knowledge_base_id)")

    op.execute("ALTER TABLE document_chunks ADD COLUMN knowledge_base_id UUID")
    op.execute("ALTER TABLE document_chunks ADD COLUMN content_hash VARCHAR(64)")
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding_model VARCHAR(100)")
    op.execute("ALTER TABLE document_chunks ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT true")
    op.execute("CREATE INDEX ix_chunks_kb ON document_chunks (knowledge_base_id)")
    op.execute("CREATE INDEX ix_chunks_content_hash ON document_chunks (content_hash)")
    op.execute("CREATE INDEX ix_chunks_is_current ON document_chunks (is_current)")

    # ---- user chat preferences + memory facts ----
    op.execute("""
        CREATE TABLE user_chat_preferences (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL,
            default_kb_ids JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_chat_prefs_tenant ON user_chat_preferences (tenant_id)")

    op.execute("""
        CREATE TABLE user_memory_facts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            fact_text VARCHAR(1000) NOT NULL,
            source VARCHAR(20) NOT NULL DEFAULT 'explicit_tool',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_memory_facts_tenant ON user_memory_facts (tenant_id)")
    op.execute("CREATE INDEX ix_memory_facts_user ON user_memory_facts (user_id)")

    # ---- RLS on every new tenant-scoped table ----
    for table in ("knowledge_bases", "projects", "messages", "user_memory_facts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        """)
    # user_chat_preferences also gets RLS despite being keyed by user_id,
    # since it carries a tenant_id column for exactly this purpose.
    op.execute("ALTER TABLE user_chat_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON user_chat_preferences
        USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
    """)
    # knowledge_base_access_grants and conversation_knowledge_bases are
    # scoped transitively via knowledge_base_id/conversation_id — same
    # subquery-based RLS pattern as user_roles in migration 0004.
    op.execute("ALTER TABLE knowledge_base_access_grants ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON knowledge_base_access_grants
        USING (
            knowledge_base_id IN (
                SELECT id FROM knowledge_bases
                WHERE tenant_id = current_setting('app.current_tenant', true)::uuid
            )
        )
    """)
    op.execute("ALTER TABLE conversation_knowledge_bases ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON conversation_knowledge_bases
        USING (
            conversation_id IN (
                SELECT id FROM conversations
                WHERE tenant_id = current_setting('app.current_tenant', true)::uuid
            )
        )
    """)

    # ---- new RBAC permissions ----
    op.execute("""
        INSERT INTO permissions (code, resource, action, description) VALUES
        ('kb.read', 'kb', 'read', 'View and use knowledge bases in chat'),
        ('kb.manage', 'kb', 'manage', 'Create/edit knowledge bases and manage access grants'),
        ('project.write', 'project', 'write', 'Create/edit chat projects'),
        ('conversation.read_all', 'conversation', 'read_all', 'View any tenant conversation (admin/support/compliance oversight)')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_memory_facts CASCADE")
    op.execute("DROP TABLE IF EXISTS user_chat_preferences CASCADE")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS is_current")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_model")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_hash")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS knowledge_base_id")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS superseded_by_id")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS knowledge_base_id")
    op.execute("DROP TABLE IF EXISTS conversation_knowledge_bases CASCADE")
    op.execute("DROP TABLE IF EXISTS messages CASCADE")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS forked_at_message_id")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS forked_from_conversation_id")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS title_generation_status")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS project_id")
    op.execute("DROP TABLE IF EXISTS projects CASCADE")
    op.execute("DROP TABLE IF EXISTS knowledge_base_access_grants CASCADE")
    op.execute("DROP TABLE IF EXISTS knowledge_bases CASCADE")
