"""Health check endpoint."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check() -> dict:
    """Basic liveness check. Returns a static healthy payload."""
    return {"status": "healthy"}
