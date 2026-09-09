from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness():
    # Extend with real DB/Redis ping checks before wiring into k8s readinessProbe.
    return {"status": "ready"}
