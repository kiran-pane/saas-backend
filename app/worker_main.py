"""Celery entrypoint: `celery -A app.worker_main.celery_app worker -Q <queue>`"""
from app.tasks.celery_app import celery_app  # noqa: F401
