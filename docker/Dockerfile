# Multistage build: small final image, non-root user
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir poetry==1.8.3
COPY pyproject.toml ./
RUN poetry config virtualenvs.create false \
    && poetry export -f requirements.txt --output requirements.txt --without-hashes --without dev || \
       pip freeze > requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
# libmagic1: required by python-magic for MIME-sniffing in the storage
# scanning gate (app/storage/scanning.py) — without this, MIME
# validation silently degrades to "unknown" for every file.
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m appuser
COPY --from=builder /install /usr/local
WORKDIR /app
COPY . .
RUN mkdir -p /var/app/storage-dev /var/app/media-import /var/app/live-hls /var/app/live-recordings \
    && chown -R appuser:appuser /var/app
RUN chmod +x docker/entrypoint.sh
USER appuser
EXPOSE 8000
# entrypoint.sh runs `alembic upgrade head` before starting uvicorn, so
# `docker compose up` produces a fully-migrated project on first run.
# Only the api service's entrypoint runs migrations (not the workers,
# see Dockerfile.worker) to avoid concurrent DDL from multiple containers.
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
