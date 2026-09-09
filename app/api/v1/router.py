from fastapi import APIRouter

from app.api.v1 import auth, dev_storage, documents, health, live_hls, llm, media, payments, users, webhooks
from app.api.v1.admin import roles as admin_roles
from app.api.v1.admin import tenants as admin_tenants

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(documents.router)
api_router.include_router(llm.router)
api_router.include_router(media.router)
api_router.include_router(admin_roles.router)
api_router.include_router(admin_tenants.router)
api_router.include_router(dev_storage.router)
api_router.include_router(payments.router)
api_router.include_router(webhooks.router)
api_router.include_router(live_hls.router)
