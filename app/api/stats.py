from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.services import app_log as app_log_svc
from app.services.usage_stats import usage_stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
def get_stats():
    return usage_stats.snapshot()


@router.post("/reset")
def reset_stats():
    usage_stats.reset()
    return {"ok": True, **usage_stats.snapshot()}


@router.get("/log")
def list_persisted_log(
    tier: str | None = Query(default=None, description="error | slow | info | omit for all"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    tier_f = (tier or "").strip().lower() or None
    if tier_f and tier_f not in app_log_svc.PERSIST_TIERS:
        raise HTTPException(400, "tier must be error, slow, info, or omitted")
    rows = app_log_svc.list_entries(tier=tier_f, limit=limit, offset=offset)
    return {
        "total": app_log_svc.count(tier=tier_f),
        "total_all": app_log_svc.count(),
        "limit": limit,
        "offset": offset,
        "tier": tier_f,
        "items": rows,
        "counts": app_log_svc.counts_by_tier(),
    }


@router.delete("/log")
def clear_persisted_log(
    tier: str | None = Query(default=None, description="error | slow | info | omit to clear all"),
):
    tier_f = (tier or "").strip().lower() or None
    if tier_f and tier_f not in app_log_svc.PERSIST_TIERS:
        raise HTTPException(400, "tier must be error, slow, info, or omitted")
    deleted = app_log_svc.clear(tier=tier_f)
    return {"ok": True, "deleted": deleted, "remaining": app_log_svc.count()}


@router.get("/log.txt")
def download_app_log(
    source: str = Query(
        default="all",
        description="ram | db | all — in-memory log, persisted app_log, or both",
    ),
):
    src = (source or "all").strip().lower()
    if src not in ("ram", "db", "all"):
        raise HTTPException(400, "source must be ram, db, or all")

    parts: list[str] = []
    if src in ("ram", "all"):
        parts.append(usage_stats.export_log_text().rstrip("\n"))
    if src in ("db", "all"):
        parts.append(app_log_svc.export_text().rstrip("\n"))
    body = "\n\n".join(p for p in parts if p) + "\n"
    suffix = {"ram": "ram", "db": "persist", "all": "all"}[src]
    return Response(
        content=body.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="cfsmcp2-app-log-{suffix}.txt"',
        },
    )
