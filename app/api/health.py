from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health():
    return {"status": "ok", "service": "cfsmcp2"}
