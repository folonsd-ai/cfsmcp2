from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import tags as tag_repo

router = APIRouter(prefix="/api/tags", tags=["tags"])


class TagOut(BaseModel):
    id: int
    name: str
    color: str = "#64748b"
    sort_order: int = 0
    entity_count: int = 0


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    color: str | None = None


class TagPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = None
    sort_order: int | None = None


class TagsEnableBody(BaseModel):
    enabled: bool
    tag_ids: list[int] = Field(default_factory=list)
    match_all: bool = False


def _out(row: dict) -> TagOut:
    return TagOut(
        id=int(row["id"]),
        name=row["name"],
        color=tag_repo.normalize_color(row.get("color")),
        sort_order=int(row.get("sort_order") or 0),
        entity_count=int(row.get("entity_count") or 0),
    )


@router.get("")
def list_tags() -> list[TagOut]:
    conn = connect(settings.db_path)
    try:
        return [_out(r) for r in tag_repo.list_tags(conn)]
    finally:
        conn.close()


@router.post("")
def create_tag(body: TagCreate) -> TagOut:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    conn = connect(settings.db_path)
    try:
        if tag_repo.get_tag_by_name(conn, name):
            raise HTTPException(409, f"Tag already exists: {name}")
        tid = tag_repo.create_tag(conn, name, color=body.color)
        conn.commit()
        row = tag_repo.get_tag(conn, tid)
        assert row
        row["entity_count"] = 0
        return _out(row)
    finally:
        conn.close()


@router.post("/filtered/enable")
def set_filtered_enabled(body: TagsEnableBody) -> dict:
    """Enable/disable entities. Empty tag_ids = all entities; otherwise match tags (OR by default).
    Disable also turns off BSL."""
    conn = connect(settings.db_path)
    try:
        if body.tag_ids:
            for tid in body.tag_ids:
                if not tag_repo.get_tag(conn, tid):
                    raise HTTPException(404, f"Tag not found: {tid}")
            ids = tag_repo.list_entity_ids_with_tags(
                conn, body.tag_ids, match_all=body.match_all
            )
        else:
            ids = [int(r["id"]) for r in ent_repo.list_entities(conn)]
        for eid in ids:
            ent = ent_repo.get_entity(conn, eid)
            if not ent:
                continue
            if body.enabled:
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
                    (eid,),
                ).fetchone()["c"]
                if int(cnt or 0) > 0:
                    ent_repo.set_status(
                        conn, eid, ent["status"], enabled=1, bsl_enabled=1
                    )
                else:
                    ent_repo.set_status(conn, eid, ent["status"], enabled=1)
            else:
                ent_repo.set_status(conn, eid, ent["status"], enabled=0, bsl_enabled=0)
        conn.commit()
        return {
            "ok": True,
            "updated": len(ids),
            "enabled": body.enabled,
            "tag_ids": body.tag_ids,
            "match_all": body.match_all,
        }
    finally:
        conn.close()


@router.patch("/{tag_id}")
def patch_tag(tag_id: int, body: TagPatch) -> TagOut:
    conn = connect(settings.db_path)
    try:
        row = tag_repo.get_tag(conn, tag_id)
        if not row:
            raise HTTPException(404, "Tag not found")
        name = body.name.strip() if body.name is not None else None
        if name is not None:
            other = tag_repo.get_tag_by_name(conn, name)
            if other and int(other["id"]) != tag_id:
                raise HTTPException(409, f"Tag already exists: {name}")
        tag_repo.update_tag(
            conn, tag_id, name=name, color=body.color, sort_order=body.sort_order
        )
        conn.commit()
        rows = {r["id"]: r for r in tag_repo.list_tags(conn)}
        return _out(rows[tag_id])
    finally:
        conn.close()


@router.delete("/{tag_id}")
def delete_tag(tag_id: int) -> dict:
    conn = connect(settings.db_path)
    try:
        row = tag_repo.get_tag(conn, tag_id)
        if not row:
            raise HTTPException(404, "Tag not found")
        tag_repo.delete_tag(conn, tag_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
