"""HLS/FFmpeg-based media streaming: media_assets (VOD/images) and
live_stream_sources (CCTV/RTSP). Both RLS-protected like every other
tenant-scoped table.

Revision ID: 0007_media_streaming
Revises: 0006_subscription_provider_index
Create Date: 2026-01-07
"""
from alembic import op

revision = "0007_media_streaming"
down_revision = "0006_subscription_provider_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE media_assets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            uploaded_by UUID NOT NULL REFERENCES users(id),
            filename VARCHAR(500) NOT NULL,
            media_type VARCHAR(20) NOT NULL DEFAULT 'video',
            source_type VARCHAR(20) NOT NULL DEFAULT 'storage',
            source_reference VARCHAR(2000) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            hls_manifest_key VARCHAR(1000),
            thumbnail_key VARCHAR(1000),
            duration_seconds DOUBLE PRECISION,
            size_bytes BIGINT,
            renditions JSONB NOT NULL DEFAULT '{}',
            failure_reason VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_media_assets_tenant ON media_assets (tenant_id)")

    op.execute("""
        CREATE TABLE live_stream_sources (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            created_by UUID NOT NULL REFERENCES users(id),
            name VARCHAR(255) NOT NULL,
            source_url VARCHAR(2000) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'stopped',
            hls_output_path VARCHAR(1000),
            last_heartbeat_at TIMESTAMPTZ,
            last_error VARCHAR(1000),
            restart_count INTEGER NOT NULL DEFAULT 0,
            record_to_storage BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_live_sources_tenant ON live_stream_sources (tenant_id)")

    for table in ("media_assets", "live_stream_sources"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
        """)

    op.execute("""
        INSERT INTO permissions (code, resource, action, description) VALUES
        ('media.read', 'media', 'read', 'View video/image assets and live streams'),
        ('media.write', 'media', 'write', 'Upload media assets and register live stream sources'),
        ('media.stream.manage', 'media.stream', 'manage', 'Start/stop live stream sources')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS live_stream_sources CASCADE")
    op.execute("DROP TABLE IF EXISTS media_assets CASCADE")
