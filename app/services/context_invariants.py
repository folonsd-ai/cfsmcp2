from __future__ import annotations

import sqlite3
from typing import Any

from app.services.context_key import normalize_context_key


class ContextKeyConflict(ValueError):
    """Entity/tag name violates normalized-key invariant."""


def _entities_by_key(conn: sqlite3.Connection) -> dict[str, list[tuple[int, str]]]:
    rows = conn.execute("SELECT id, name FROM entities").fetchall()
    out: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        key = normalize_context_key(str(r["name"]))
        out.setdefault(key, []).append((int(r["id"]), str(r["name"])))
    return out


def _tags_by_key(conn: sqlite3.Connection) -> dict[str, list[tuple[int, str]]]:
    rows = conn.execute("SELECT id, name FROM tags").fetchall()
    out: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        key = normalize_context_key(str(r["name"]))
        out.setdefault(key, []).append((int(r["id"]), str(r["name"])))
    return out


def assert_entity_name_allowed(
    conn: sqlite3.Connection,
    name: str,
    *,
    exclude_entity_id: int | None = None,
) -> None:
    raw = (name or "").strip()
    if not raw:
        raise ContextKeyConflict("Name must not be empty")
    key = normalize_context_key(raw)
    for eid, ename in _entities_by_key(conn).get(key, []):
        if exclude_entity_id is not None and eid == exclude_entity_id:
            continue
        raise ContextKeyConflict(
            f"Context name {raw!r} conflicts with existing context {ename!r} (same normalized key)"
        )
    for _tid, tname in _tags_by_key(conn).get(key, []):
        raise ContextKeyConflict(
            f"Context name {raw!r} conflicts with tag {tname!r} (same normalized key)"
        )


def assert_tag_name_allowed(
    conn: sqlite3.Connection,
    name: str,
    *,
    exclude_tag_id: int | None = None,
) -> None:
    raw = (name or "").strip()
    if not raw:
        raise ContextKeyConflict("Tag name must not be empty")
    key = normalize_context_key(raw)
    for tid, tname in _tags_by_key(conn).get(key, []):
        if exclude_tag_id is not None and tid == exclude_tag_id:
            continue
        raise ContextKeyConflict(
            f"Tag name {raw!r} conflicts with existing tag {tname!r} (same normalized key)"
        )
    for _eid, ename in _entities_by_key(conn).get(key, []):
        raise ContextKeyConflict(
            f"Tag name {raw!r} conflicts with context {ename!r} (same normalized key)"
        )


def find_legacy_key_violations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    ekeys = _entities_by_key(conn)
    tkeys = _tags_by_key(conn)
    for key, ents in ekeys.items():
        if len(ents) > 1:
            issues.append({"kind": "entities", "key": key, "names": [n for _, n in ents]})
    for key, tags in tkeys.items():
        if len(tags) > 1:
            issues.append({"kind": "tags", "key": key, "names": [n for _, n in tags]})
    for key in set(ekeys) & set(tkeys):
        issues.append(
            {
                "kind": "cross",
                "key": key,
                "entities": [n for _, n in ekeys[key]],
                "tags": [n for _, n in tkeys[key]],
            }
        )
    return issues
