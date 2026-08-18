from __future__ import annotations

import json
import sqlite3
from typing import Any


def comment_from_name_version(name: str, version: str) -> str:
    """Default entity comment = report Имя + Версия."""
    parts = [((name or "").strip()), ((version or "").strip())]
    return " ".join(p for p in parts if p)


def list_entities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # Use stored entities.bsl_method_count — a live COUNT(*) over objects
    # stalls reindex (WAL still fights for disk; UI polls every few seconds).
    rows = conn.execute(
        """
        SELECT e.*
        FROM entities e
        ORDER BY e.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_entity(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    return dict(row) if row else None


def get_entity_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def suggest_unique_name(conn: sqlite3.Connection, base: str) -> str:
    """Return ``base`` or ``base_2`` / ``base_3`` / … that is not yet used."""
    raw = (base or "").strip() or "config"
    if not get_entity_by_name(conn, raw):
        return raw
    for i in range(2, 1000):
        cand = f"{raw}_{i}"
        if not get_entity_by_name(conn, cand):
            return cand
    return f"{raw}_new"


def ingest_profile_of(entity: dict) -> dict:
    """Parsed ingest_profile JSON (lenient; {} on any error)."""
    raw = entity.get("ingest_profile") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def upsert_entity(
    conn: sqlite3.Connection,
    *,
    name: str,
    synonym: str,
    version: str,
    file_path: str,
    model: str,
    entity_type: str = "configuration",
    name_locked: bool = False,
    source_mode: str = "report",
    source_location: str = "upload",
    source_path: str = "",
    ingest_profile: dict | None = None,
    dumps_dir: str = "",
    zip_path: str = "",
    comment: str | None = None,
) -> int:
    """Register or refresh entity for a new upload. Keeps existing objects for incremental parse.

    name_locked=True: keep this context name forever (do not rename from report meta).
    Used for multiple versions of the same configuration under different MCP contexts.

    cfsmcp2 (§6): source_mode/source_location/source_path/ingest_profile сохраняются
    при создании/обновлении из upload. Профиль фиксируется при создании сущности
    (на «Обновить» не меняется — см. refresh_entity_file).

    cfsmcp2 (§15 п.3): для dump-режима ``dumps_dir``/``zip_path`` сохраняются
    (upload: dumps/e{id}/ и e{id}.zip; path: внешний каталог и '').
    """
    locked = 1 if name_locked else 0
    profile_json = json.dumps(ingest_profile or {}, ensure_ascii=False)
    provided_comment = (comment or "").strip() if comment is not None else ""
    existing = get_entity_by_name(conn, name)
    if existing:
        if existing.get("model") != model:
            conn.execute(
                "UPDATE objects SET embed_done=0 WHERE entity_id=?",
                (existing["id"],),
            )
            conn.execute(
                "DELETE FROM pending_zvec_deletes WHERE entity_id=?",
                (existing["id"],),
            )
        # Explicit override locks the name; otherwise keep previous lock flag
        new_locked = 1 if name_locked else int(existing.get("name_locked") or 0)
        if provided_comment:
            new_comment = provided_comment
        else:
            auto_comment = comment_from_name_version(name, version)
            old_auto = comment_from_name_version(
                existing.get("name") or "", existing.get("version") or ""
            )
            prev_comment = (existing.get("comment") or "").strip()
            # Refresh auto comment unless user customized it
            new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, file_path=?, model=?,
              entity_type=?, name_locked=?, comment=?, status='uploaded', indexed_count=0, index_target=0,
              source_mode=?, source_location=?, source_path=?, ingest_profile=?,
              dumps_dir=?, zip_path=?,
              error_message='', updated_at=datetime('now')
            WHERE id=?
            """,
            (
                synonym,
                version,
                file_path,
                model,
                entity_type,
                new_locked,
                new_comment,
                source_mode,
                source_location,
                source_path,
                profile_json,
                dumps_dir,
                zip_path,
                existing["id"],
            ),
        )
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO entities(
          name, synonym, comment, entity_type, version, file_path, model, name_locked, status,
          source_mode, source_location, source_path, ingest_profile, dumps_dir, zip_path
        )
        VALUES (?,?,?,?,?,?,?,?, 'uploaded', ?,?,?,?,?,?)
        """,
        (
            name,
            synonym,
            provided_comment or comment_from_name_version(name, version),
            entity_type,
            version,
            file_path,
            model,
            locked,
            source_mode,
            source_location,
            source_path,
            profile_json,
            dumps_dir,
            zip_path,
        ),
    )
    return int(cur.lastrowid)


def refresh_entity_file(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    synonym: str,
    version: str,
    file_path: str,
    model: str,
    entity_type: str | None = None,
    source_path: str | None = None,
    source_location: str | None = None,
    source_mode: str | None = None,
    dumps_dir: str | None = None,
    zip_path: str | None = None,
    comment: str | None = None,
) -> int:
    """Replace report file for an existing entity (row upload / merge). Keeps name and name_locked.

    cfsmcp2: source_mode/source_location/ingest_profile не меняются при обновлении,
    если не переданы явно. ``source_location`` передаётся из import-path (этап 2.5),
    чтобы сущность, обновлённая по внешнему пути, стала path-режимом (иначе guard
    на удаление счёл бы внешний ``file_path`` своим). source_path обновляется
    новым именем файла, если передан. ``source_mode``/``dumps_dir``/``zip_path``
    передаются из upload-dump / import-path dump (этап 3).
    """
    existing = get_entity(conn, entity_id)
    if not existing:
        raise KeyError(f"entity {entity_id}")
    if existing.get("model") != model:
        conn.execute(
            "UPDATE objects SET embed_done=0 WHERE entity_id=?",
            (entity_id,),
        )
        conn.execute(
            "DELETE FROM pending_zvec_deletes WHERE entity_id=?",
            (entity_id,),
        )
    provided_comment = (comment or "").strip() if comment is not None else ""
    if provided_comment:
        new_comment = provided_comment
    else:
        auto_comment = comment_from_name_version(existing.get("name") or "", version)
        old_auto = comment_from_name_version(
            existing.get("name") or "", existing.get("version") or ""
        )
        prev_comment = (existing.get("comment") or "").strip()
        new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment
    etype = (entity_type or existing.get("entity_type") or "configuration").strip() or "configuration"
    fields = [
        "synonym=?", "version=?", "file_path=?", "model=?",
        "comment=?", "entity_type=?",
    ]
    values: list[Any] = [synonym, version, file_path, model, new_comment, etype]
    if source_path is not None:
        fields.append("source_path=?")
        values.append(source_path)
    if source_location is not None:
        fields.append("source_location=?")
        values.append(source_location)
    if source_mode is not None:
        fields.append("source_mode=?")
        values.append(source_mode)
    if dumps_dir is not None:
        fields.append("dumps_dir=?")
        values.append(dumps_dir)
    if zip_path is not None:
        fields.append("zip_path=?")
        values.append(zip_path)
    fields.extend(
        [
            "status='uploaded'",
            "indexed_count=0",
            "index_target=0",
            "error_message=''",
            "updated_at=datetime('now')",
        ]
    )
    values.append(entity_id)
    conn.execute(f"UPDATE entities SET {', '.join(fields)} WHERE id=?", values)
    return entity_id


def set_entity_type(conn: sqlite3.Connection, entity_id: int, entity_type: str) -> None:
    et = (entity_type or "").strip().lower()
    if et not in {"configuration", "extension"}:
        et = "configuration"
    conn.execute(
        "UPDATE entities SET entity_type=?, updated_at=datetime('now') WHERE id=?",
        (et, entity_id),
    )


def set_comment(conn: sqlite3.Connection, entity_id: int, comment: str) -> None:
    conn.execute(
        """
        UPDATE entities SET comment=?, updated_at=datetime('now') WHERE id=?
        """,
        ((comment or "").strip(), entity_id),
    )


def update_source_path(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    source_path: str,
    file_path: str | None = None,
    dumps_dir: str | None = None,
    zip_path: str | None = None,
) -> None:
    """Обновить путь источника без смены статуса / без ingest.

    Для path-режима обычно передают и ``file_path``/``dumps_dir`` (тот же внешний
    каталог после смены точки подключения). Для upload — только ``source_path``
    (закладка для следующей загрузки).
    """
    fields = ["source_path=?"]
    values: list[Any] = [source_path]
    if file_path is not None:
        fields.append("file_path=?")
        values.append(file_path)
    if dumps_dir is not None:
        fields.append("dumps_dir=?")
        values.append(dumps_dir)
    if zip_path is not None:
        fields.append("zip_path=?")
        values.append(zip_path)
    fields.append("updated_at=datetime('now')")
    values.append(entity_id)
    conn.execute(f"UPDATE entities SET {', '.join(fields)} WHERE id=?", values)


_BUSY_STATUSES = frozenset({"parsing", "indexing", "uploaded", "loading_modules"})


def rename_entity(conn: sqlite3.Connection, entity_id: int, new_name: str) -> dict[str, Any]:
    """Rename MCP context. Sets name_locked=1. Files/zvec stay on entity id."""
    name = (new_name or "").strip()
    if not name:
        raise ValueError("Name must not be empty")
    if len(name) > 200:
        raise ValueError("Name is too long (max 200 characters)")
    row = get_entity(conn, entity_id)
    if not row:
        raise KeyError(f"entity {entity_id}")
    if row["status"] in _BUSY_STATUSES:
        raise ValueError(f"Entity is busy (status={row['status']})")
    old_name = row["name"]
    if old_name == name:
        return dict(row)
    other = get_entity_by_name(conn, name)
    if other and int(other["id"]) != int(entity_id):
        raise ValueError(f"Name already used: {name}")

    version = row.get("version") or ""
    auto_old = comment_from_name_version(old_name, version)
    auto_new = comment_from_name_version(name, version)
    prev = (row.get("comment") or "").strip()
    new_comment = auto_new if (not prev or prev == auto_old) else prev

    try:
        from app.services.search import invalidate_entity_cache

        invalidate_entity_cache(old_name)
    except Exception:
        pass

    conn.execute(
        """
        UPDATE entities
        SET name=?, name_locked=1, comment=?, updated_at=datetime('now')
        WHERE id=?
        """,
        (name, new_comment, entity_id),
    )
    try:
        from app.services.search import invalidate_entity_cache

        invalidate_entity_cache(name)
    except Exception:
        pass
    updated = get_entity(conn, entity_id)
    return dict(updated) if updated else {"id": entity_id, "name": name}


def claim_indexing(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    indexed_count: int,
    index_target: int,
    index_started_at: float,
    index_scope: str,
    bump_bsl_gen: bool = False,
) -> bool:
    """Atomically take the indexing slot. Returns False if already indexing/parsing.

    Generation bump is in the same UPDATE so two HTTP requests cannot both increment
    ``bsl_embed_gen`` before either row becomes ``indexing``.
    """
    cur = conn.execute(
        """
        UPDATE entities
        SET status='indexing',
            error_message='',
            indexed_count=?,
            index_target=?,
            index_started_at=?,
            index_scope=?,
            bsl_embed_gen = CASE WHEN ? THEN COALESCE(bsl_embed_gen, 0) + 1
                                 ELSE bsl_embed_gen END,
            updated_at=datetime('now')
        WHERE id=?
          AND status NOT IN ('indexing', 'parsing')
          AND (object_count > 0 OR status IN ('parsed', 'ready', 'index_error'))
        """,
        (
            int(indexed_count),
            int(index_target),
            float(index_started_at),
            str(index_scope or ""),
            1 if bump_bsl_gen else 0,
            int(entity_id),
        ),
    )
    if (cur.rowcount or 0) <= 0:
        return False
    _invalidate_search_cache(conn, entity_id)
    return True


def set_status(
    conn: sqlite3.Connection,
    entity_id: int,
    status: str,
    *,
    error_message: str | None = None,
    object_count: int | None = None,
    link_count: int | None = None,
    indexed_count: int | None = None,
    index_target: int | None = None,
    index_started_at: float | None = None,
    index_scope: str | None = None,
    model: str | None = None,
    enabled: int | None = None,
    bsl_enabled: int | None = None,
    bsl_load_mode: str | None = None,
    bsl_method_count: int | None = None,
    parse_gen: int | None = None,
    parse_added: int | None = None,
    parse_changed: int | None = None,
    parse_deleted: int | None = None,
    parse_unchanged: int | None = None,
) -> None:
    fields = ["status=?", "updated_at=datetime('now')"]
    vals: list[Any] = [status]
    mapping = [
        ("error_message", error_message),
        ("object_count", object_count),
        ("link_count", link_count),
        ("indexed_count", indexed_count),
        ("index_target", index_target),
        ("index_started_at", index_started_at),
        ("index_scope", index_scope),
        ("model", model),
        ("enabled", enabled),
        ("bsl_enabled", bsl_enabled),
        ("bsl_load_mode", bsl_load_mode),
        ("bsl_method_count", bsl_method_count),
        ("parse_gen", parse_gen),
        ("parse_added", parse_added),
        ("parse_changed", parse_changed),
        ("parse_deleted", parse_deleted),
        ("parse_unchanged", parse_unchanged),
    ]
    for col, val in mapping:
        if val is not None:
            fields.append(f"{col}=?")
            vals.append(val)
    vals.append(entity_id)
    conn.execute(f"UPDATE entities SET {', '.join(fields)} WHERE id=?", vals)
    _invalidate_search_cache(conn, entity_id)


def set_bsl_load_mode(conn: sqlite3.Connection, entity_id: int, mode: str) -> None:
    conn.execute(
        "UPDATE entities SET bsl_load_mode=?, updated_at=datetime('now') WHERE id=?",
        ((mode or "").strip(), entity_id),
    )


def set_bsl_embed_mode(conn: sqlite3.Connection, entity_id: int, mode: str) -> None:
    conn.execute(
        "UPDATE entities SET bsl_embed_mode=?, updated_at=datetime('now') WHERE id=?",
        ((mode or "").strip(), entity_id),
    )


def delete_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    _invalidate_search_cache(conn, entity_id)
    conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))


def _invalidate_search_cache(conn: sqlite3.Connection, entity_id: int) -> None:
    """Drop MCP resolve_context TTL entry for this entity (lazy import)."""
    try:
        row = conn.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
        name = row["name"] if row else None
        from app.services.search import invalidate_entity_cache

        invalidate_entity_cache(name)
    except Exception:
        pass


def list_ready_contexts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from app.schemas.entities import ingest_profile_flags

    rows = conn.execute(
        """
        SELECT e.id, e.name, e.synonym, e.version, e.model, e.object_count,
          e.source_mode, e.bsl_method_count, e.ingest_profile, e.link_count,
          (
            SELECT GROUP_CONCAT(t.name, char(31))
            FROM entity_tags et
            JOIN tags t ON t.id = et.tag_id
            WHERE et.entity_id = e.id
          ) AS tags_joined
        FROM entities e
        WHERE e.enabled=1 AND e.status='ready'
        ORDER BY e.name
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.pop("tags_joined", None) or ""
        d["tags"] = [p for p in str(raw).split("\x1f") if p] if raw else []
        profile = ingest_profile_of(d)
        d.pop("ingest_profile", None)
        flags = ingest_profile_flags(profile)
        # report = всегда без BSL; lean dump — пресет «только метаданные»
        if str(d.get("source_mode") or "").lower() == "report":
            flags["lean"] = True
            flags["bsl"] = False
            flags["help"] = False
        d["profile"] = flags
        d["bsl_method_count"] = int(d.get("bsl_method_count") or 0)
        d["link_count"] = int(d.get("link_count") or 0)
        out.append(d)
    return out


def list_ready_entities_for_tag(conn: sqlite3.Connection, tag_name: str) -> list[dict[str, Any]]:
    """Ready+enabled entities that have the given tag (by tag name, case-sensitive match as stored)."""
    name = (tag_name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.synonym, e.version, e.model, e.object_count,
               e.enabled, e.status, e.bsl_enabled
        FROM entities e
        JOIN entity_tags et ON et.entity_id = e.id
        JOIN tags t ON t.id = et.tag_id
        WHERE e.enabled=1 AND e.status='ready' AND t.name = ?
        ORDER BY e.name
        """,
        (name,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_entities_with_tag(conn: sqlite3.Connection, tag_name: str) -> list[dict[str, Any]]:
    """Все сущности с тегом (без фильтра enabled/ready) — для get_indexing_status."""
    name = (tag_name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        """
        SELECT e.*, 0 AS bsl_method_count
        FROM entities e
        JOIN entity_tags et ON et.entity_id = e.id
        JOIN tags t ON t.id = et.tag_id
        WHERE t.name = ?
        ORDER BY e.name
        """,
        (name,),
    ).fetchall()
    return [dict(r) for r in rows]
