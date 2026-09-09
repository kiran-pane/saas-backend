from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENV: str = "development"
    APP_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    # See app/core/db/session.py::system_engine — only needed for the
    # live-stream supervisor's genuinely cross-tenant queries. Defaults to
    # DATABASE_URL; override with a BYPASSRLS-role connection string in a
    # hardened deployment where the main app role is RLS-restricted.
    SYSTEM_DATABASE_URL: str | None = None

    REDIS_URL: str = "redis://localhost:6379/0"

    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str | None = None
    LANGCHAIN_PROJECT: str = "saas-backend"

    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    DEFAULT_LLM_MODEL: str = "openai:gpt-4.1-mini"
    DEFAULT_LLM_FALLBACKS: list[str] = ["anthropic:claude-sonnet"]

    # ---- Google OAuth (Sign in with Google) ----
    # GOOGLE_CLIENT_ID is required to verify ID tokens sent from the
    # frontend (the "audience" check). GOOGLE_CLIENT_SECRET/REDIRECT_URI
    # are only needed if you also use the server-driven authorization-code
    # flow (see app/core/security/google_oauth.py) rather than having the
    # frontend hand you an ID token directly.
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str | None = None

    # ---- Frontend / CORS ----
    FRONTEND_URL: str = "http://localhost:3000"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
    CORS_ALLOW_CREDENTIALS: bool = True

    # ---- Data seeding ----
    # SEED_PERMISSIONS_ON_STARTUP: if true, the api container's entrypoint
    # runs `python -m app.seeds.run permissions` after migrations, on every
    # start. Safe to leave on — it's a pure idempotent upsert (see
    # app/seeds/permissions.py) — but off by default so nothing runs
    # implicitly that you haven't opted into.
    SEED_PERMISSIONS_ON_STARTUP: bool = False

    # SEED_TENANT_SLUG unset (default) = no dev/demo tenant is ever
    # created, in any environment. Set all three SEED_* below together to
    # bootstrap a demo tenant + owner user via `python -m app.seeds.run dev-data`
    # (idempotent — a second run is a no-op if the tenant already exists).
    SEED_TENANT_SLUG: str | None = None
    SEED_TENANT_NAME: str | None = None
    SEED_ADMIN_EMAIL: str | None = None
    SEED_ADMIN_PASSWORD: str | None = None

    # ---- Storage abstraction layer ----
    STORAGE_PROVIDER: str = "local"  # local | s3 | gcs | azure | cloudinary
    STORAGE_MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024   # 200MB single-shot cap; above this, use multipart
    STORAGE_PRESIGNED_URL_DEFAULT_TTL_SECONDS: int = 900

    S3_BUCKET: str | None = None
    S3_REGION: str = "us-east-1"
    S3_ENDPOINT_URL: str | None = None   # set for MinIO/R2/self-hosted S3-compatible; None = real AWS
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_KMS_KEY_ID: str | None = None

    GCS_BUCKET: str | None = None
    GCS_CREDENTIALS_PATH: str | None = None
    GCS_KMS_KEY_NAME: str | None = None

    AZURE_ACCOUNT_URL: str | None = None
    AZURE_CONTAINER: str | None = None
    AZURE_ACCOUNT_KEY: str | None = None

    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None

    LOCAL_STORAGE_ROOT: str = "/var/app/storage-dev"

    # ---- Virus scanning (ClamAV) ----
    CLAMAV_HOST: str = "clamav"
    CLAMAV_PORT: int = 3310
    CLAMAV_ENABLED: bool = True   # disable only in local dev without a clamd container running

    # ---- Storage lifecycle defaults (per-tenant override via tenant.settings["storage_lifecycle"]) ----
    DEFAULT_OLD_VERSION_RETENTION_DAYS: int = 30
    DEFAULT_ARCHIVE_AFTER_DAYS: int = 180

    # ---- Audit log retention ----
    AUDIT_LOG_RETENTION_MONTHS: int = 24

    # ---- HLS/FFmpeg media streaming ----
    FFMPEG_BINARY_PATH: str = "/usr/bin/ffmpeg"
    FFMPEG_VOD_TIMEOUT_SECONDS: int = 3600
    LOCAL_MEDIA_IMPORT_ROOT: str = "/var/app/media-import"   # admin-controlled root for source_type="local_disk"
    LIVE_HLS_OUTPUT_ROOT: str = "/var/app/live-hls"          # shared volume, served by the API/nginx
    LIVE_RECORDING_TEMP_ROOT: str = "/var/app/live-recordings"
    LIVE_STREAM_MAX_RESTARTS: int = 5
    LIVE_STREAM_POLL_INTERVAL_SECONDS: int = 5

    # ---- Payments ----
    PAYMENT_PROVIDER: str = "stripe"  # stripe | razorpay | paypal — default when no region override applies

    STRIPE_SECRET_KEY: str | None = None
    STRIPE_WEBHOOK_SECRET: str | None = None

    RAZORPAY_KEY_ID: str | None = None
    RAZORPAY_KEY_SECRET: str | None = None
    RAZORPAY_WEBHOOK_SECRET: str | None = None

    PAYPAL_CLIENT_ID: str | None = None
    PAYPAL_CLIENT_SECRET: str | None = None
    PAYPAL_WEBHOOK_ID: str | None = None
    PAYPAL_SANDBOX: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
