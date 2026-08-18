from __future__ import annotations

import re
import sqlite3
from typing import Any

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_TAG_COLOR = "#64748b"


def normalize_color(color: str | None) -> str:
    c = (color or "").strip()
    if _HEX.match(c):
        return c.lower()
    return DEFAULT_TAG_COLOR


def list_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.*,
          (SELECT COUNT(*) FROM entity_tags et WHERE et.tag_id=t.id) AS entity_count
        FROM tags t
        ORDER BY t.sort_order, t.name
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["color"] = normalize_color(d.get("color"))
        out.append(d)
    return out


def get_tag(conn: sqlite3.Connection, tag_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["color"] = normalize_color(d.get("color"))
    return d


def get_tag_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tags WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["color"] = normalize_color(d.get("color"))
    return d


def create_tag(
    conn: sqlite3.Connection,
    name: str,
    *,
    color: str | None = None,
    sort_order: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO tags(name, color, sort_order) VALUES (?,?,?)",
        (name, normalize_color(color), sort_order),
    )
    return int(cur.lastrowid)


def update_tag(
    conn: sqlite3.Connection,
    tag_id: int,
    *,
    name: str | None = None,
    color: str | None = None,
    sort_order: int | None = None,
) -> None:
    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        sets.append("name=?")
        vals.append(name)
    if color is not None:
        sets.append("color=?")
        vals.append(normalize_color(color))
    if sort_order is not None:
        sets.append("sort_order=?")
        vals.append(sort_order)
    if not sets:
        return
    sets.append("updated_at=datetime('now')")
    vals.append(tag_id)
    conn.execute(f"UPDATE tags SET {', '.join(sets)} WHERE id=?", vals)


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> None:
    conn.execute("DELETE FROM entity_tags WHERE tag_id=?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))


def list_tag_ids_for_entity(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT tag_id FROM entity_tags WHERE entity_id=? ORDER BY tag_id",
        (entity_id,),
    ).fetchall()
    return [int(r["tag_id"]) for r in rows]


def tags_by_entity_ids(
    conn: sqlite3.Connection, entity_ids: list[int]
) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {i: [] for i in entity_ids}
    if not entity_ids:
        return out
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"""
        SELECT entity_id, tag_id FROM entity_tags
        WHERE entity_id IN ({placeholders})
        ORDER BY entity_id, tag_id
        """,
        entity_ids,
    ).fetchall()
    for r in rows:
        eid = int(r["entity_id"])
        out.setdefault(eid, []).append(int(r["tag_id"]))
    return out


def set_entity_tags(conn: sqlite3.Connection, entity_id: int, tag_ids: list[int]) -> None:
    conn.execute("DELETE FROM entity_tags WHERE entity_id=?", (entity_id,))
    uniq = sorted({int(t) for t in tag_ids})
    if not uniq:
        return
    conn.executemany(
        "INSERT INTO entity_tags(entity_id, tag_id) VALUES (?,?)",
        [(entity_id, tid) for tid in uniq],
    )


def copy_entity_tags(
    conn: sqlite3.Connection, source_id: int, dest_id: int
) -> None:
    rows = conn.execute(
        "SELECT tag_id FROM entity_tags WHERE entity_id=?",
        (source_id,),
    ).fetchall()
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO entity_tags(entity_id, tag_id) VALUES (?,?)",
        [(dest_id, int(r["tag_id"])) for r in rows],
    )


def list_entity_ids_with_tags(
    conn: sqlite3.Connection,
    tag_ids: list[int],
    *,
    match_all: bool = False,
) -> list[int]:
    """Return entity ids that have the given tags (OR by default; AND if match_all)."""
    ids = [int(t) for t in tag_ids]
    if not ids:
        return []
    if match_all:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT entity_id FROM entity_tags
            WHERE tag_id IN ({placeholders})
            GROUP BY entity_id
            HAVING COUNT(DISTINCT tag_id) = ?
            """,
            (*ids, len(ids)),
        ).fetchall()
    else:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT entity_id FROM entity_tags
            WHERE tag_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    return [int(r["entity_id"]) for r in rows]


def list_context_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tags that label at least one ready+enabled context, with member context names."""
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.color,
          GROUP_CONCAT(e.name, char(31)) AS contexts_joined,
          COUNT(e.id) AS context_count
        FROM tags t
        JOIN entity_tags et ON et.tag_id = t.id
        JOIN entities e ON e.id = et.entity_id AND e.enabled=1 AND e.status='ready'
        GROUP BY t.id, t.name, t.color
        HAVING COUNT(e.id) > 0
        ORDER BY t.sort_order, t.name
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("contexts_joined", None) or ""
        contexts = [p for p in str(raw).split("\x1f") if p]
        contexts.sort()
        out.append(
            {
                "tag": d["name"],
                "tag_id": int(d["id"]),
                "color": normalize_color(d.get("color")),
                "context_count": int(d["context_count"] or len(contexts)),
                "contexts": contexts,
                "context_ref": f"tag:{d['name']}",
            }
        )
    return out
