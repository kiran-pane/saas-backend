# SaaS Backend

Multi-tenant FastAPI backend with hierarchical RBAC, Postgres RLS tenant
isolation, Redis caching/rate-limiting, Celery background processing, and
a LangChain/LangGraph/LangSmith LLM integration layer.

See `BACKEND_ARCHITECTURE_BLUEPRINT.md` (in the parent deliverable) for
the full design rationale. This README covers day-to-day dev commands.

## Quick start (Docker)

```bash
cp .env.example .env    # fill in APP_SECRET_KEY, OPENAI_API_KEY, etc.
docker compose up -d postgres pgbouncer redis
docker compose run --rm api alembic upgrade head
docker compose up -d
```

API: http://localhost:8000/docs
Flower (Celery monitoring): http://localhost:5555

## Local (non-Docker) dev

```bash
poetry install
cp .env.example .env
# point DATABASE_URL at a local Postgres with pgvector installed
alembic upgrade head
uvicorn app.main:app --reload
```

## Google Sign-In setup

1. Create OAuth credentials at https://console.cloud.google.com/apis/credentials
   (type: "Web application"). Add your frontend's origin to "Authorized
   JavaScript origins" (e.g. `http://localhost:3000`).
2. Put the client ID in `.env` as `GOOGLE_CLIENT_ID`. That's the only
   required value if your frontend uses Google's Identity Services JS SDK
   and hands you an ID token — see `POST /api/v1/auth/google` below.
3. Only set `GOOGLE_CLIENT_SECRET` + `GOOGLE_REDIRECT_URI` if you want the
   *backend* to drive the OAuth redirect itself instead (no frontend SDK)
   — that flow is `POST /api/v1/auth/google/code`.

```bash
curl -X POST http://localhost:8000/api/v1/auth/google \
  -H "X-Tenant-Slug: acme" -H "Content-Type: application/json" \
  -d '{"id_token": "<the id_token from Google Identity Services>"}'
# -> {"access_token": "...", "refresh_token": "..."}
```

The backend verifies the token's signature, issuer, and audience against
Google's public keys before trusting anything in it — the frontend is
never trusted to assert who the user is. A user can hold both a password
and a linked Google identity; signing in with Google first links to an
existing password account matched by email, it never silently creates a
duplicate account.

## Creating your first tenant + user

```bash
# 1. Create a tenant (platform-level route, no tenant header needed)
curl -X POST http://localhost:8000/api/v1/platform/tenants \
  -H "Content-Type: application/json" \
  -d '{"slug": "acme", "name": "Acme Inc"}'

# 2. All subsequent requests must include X-Tenant-Slug (or hit
#    acme.yourapp.com in production where subdomain resolution applies)
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "X-Tenant-Slug: acme" -H "Content-Type: application/json" \
  -d '{"email": "owner@acme.com", "password": "..."}'
```

(User registration endpoint intentionally left for you to wire per your
signup flow — e.g. invite-only vs self-serve — since that's product
policy, not infrastructure.)

## Running migrations

```bash
alembic revision -m "add something"     # new migration
alembic upgrade head                     # apply
alembic downgrade -1                     # rollback one
```

## Running Celery workers locally

```bash
celery -A app.worker_main.celery_app worker -Q audit,notifications,llm --concurrency=4
celery -A app.worker_main.celery_app worker -Q llm_heavy --concurrency=2
celery -A app.worker_main.celery_app beat
```

## Full command reference

See [`docs/COMMANDS.md`](docs/COMMANDS.md) for every command you'll need:
Docker Compose (build/up/logs/scaling/shell access), Alembic migrations,
running Uvicorn directly, all Celery worker/beat/flower/inspect commands,
database and Redis maintenance, and an end-to-end smoke-test walkthrough.

## Video/CCTV streaming (HLS/FFmpeg)

**VOD:** `POST /api/v1/media/videos` registers a video from an already-
uploaded storage object, a local-disk import path, or a remote URL, and
triggers ffmpeg transcoding into an adaptive-bitrate HLS ladder (1080p/
720p/480p/360p) on the dedicated `media` Celery queue. Output is stored
through the same storage abstraction as documents. Get a presigned
playback URL via `GET /api/v1/media/videos/{id}/playlist-url`.

**Live/CCTV:** `POST /api/v1/media/live-sources` registers an RTSP/RTMP
source (validated against an SSRF guard — see `app/streaming/security.py`
— before the server ever connects to it). `POST .../start` and
`POST .../stop` flip a status flag that a dedicated, always-running
`streaming-worker` process (NOT a Celery task — see
`app/streaming_worker_main.py`) polls and reconciles by supervising an
ffmpeg subprocess per active source, with automatic crash-restart and a
rolling local HLS output window. Live segments are served directly from
a shared volume (`GET /api/v1/media/live-hls/{tenant}/{source}/...`) —
put nginx/a CDN in front of that route for real production traffic.

## Project layout

See the top of `app/` — layering is `api -> services -> repositories ->
models`, with `core/` holding cross-cutting infrastructure (db, security,
tenancy, cache, ratelimit, logging, observability), `llm/` holding the
LangChain/LangGraph/LangSmith integration (providers, graphs, ingestion),
`storage/` holding the multi-provider document storage abstraction
(S3/GCS/Azure/Cloudinary/local + virus scanning + multipart upload), and
`payments/` holding the multi-gateway payment abstraction (Stripe/
Razorpay/PayPal).

## Document storage

Upload documents via `POST /api/v1/documents/upload` (small files) or the
multipart flow (`POST /api/v1/documents/uploads/multipart` + part uploads
+ complete) for large files. Every upload is quarantined, MIME-checked,
virus-scanned (ClamAV), and only then promoted to its final tenant-scoped
path — which automatically triggers chunking/embedding. Switch providers
via `STORAGE_PROVIDER=s3|gcs|azure|cloudinary|local` in `.env`. See
`docs/COMMANDS.md` and `STORAGE_ABSTRACTION_DESIGN.md` for full detail.

## Payments

`GET /api/v1/billing/plans`, `POST /api/v1/billing/checkout` (creates a
provider checkout session — Stripe/Razorpay/PayPal, selectable per-region
or via `PAYMENT_PROVIDER`), `POST /api/v1/billing/subscriptions/{id}/cancel`.
Webhooks land on `POST /api/v1/webhooks/payments/{provider}` — this is
the source of truth for subscription/payment state, never the client-side
redirect. Configure gateway credentials in `.env` (`STRIPE_*`,
`RAZORPAY_*`, `PAYPAL_*`).

## What's stubbed vs. production-ready

Production-ready: schema + RLS (including roles/user_roles), RBAC
hierarchy + caching, JWT auth + refresh rotation + Google Sign-In, rate
limiting (including per-email login brute-force limiting and
`/auth/refresh`), audit logging (including failed logins, refresh-token
reuse, tenant/user creation, role changes, with a dead-letter queue for
outage resilience), Celery queue topology, Docker/PgBouncer/ClamAV setup,
6 chunking strategies, the full storage abstraction layer (5 providers,
virus scanning, multipart upload, lifecycle policies), the full payments
abstraction layer (3 gateways, idempotent webhook processing), data
seeding, and a unified error-response envelope across every error path
(validation errors, HTTP exceptions, storage errors, app errors).

Deliberately left as integration points (business-specific, not
infrastructure, or requiring product/legal decisions rather than more
code): platform-admin auth gating on `/platform/*` routes, user
self-registration policy, MFA (schema column exists, no flow), GDPR
right-to-deletion workflow, and — since a secrets manager choice is
infra-specific — secrets are environment-variable-based throughout
rather than Vault/Secrets-Manager-integrated. See
`SECURITY_COMPLIANCE_AUDIT.md` for the full gap analysis this list is
drawn from.
# saas-backend
