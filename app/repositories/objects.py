from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable


def content_hash(fields: dict[str, Any]) -> str:
    """Stable hash of fields that affect search / embeddings."""
    props = fields.get("props") or {}
    if isinstance(props, str):
        try:
            props = json.loads(props)
        except json.JSONDecodeError:
            props = {}
    payload = {
        "path": fields.get("path") or "",
        "kind": fields.get("kind") or "",
        "name": fields.get("name") or "",
        "synonym": fields.get("synonym") or "",
        "comment": fields.get("comment") or "",
        "belong": fields.get("belong") or "Own",
        "base_object": fields.get("base_object") or "",
        "props": props,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# SQLite drops the leading-anchor advantage of ``path LIKE 'pref.%'`` when the
# planner only uses ``entity_id``, turning every prefixed query into a full scan
# of the entity's path index (e.g. УправлениеПредприятием ~540k objects → ~20s).
# A BETWEEN range on idx_objects_path forces a true index range scan.
def path_range_params(pref: str) -> tuple[str, str]:
    return (pref, pref + chr(0x10FFFF))


def name_eq_variants(name: str, *, max_n: int = 4) -> list[str]:
    """Cheap case forms for indexed ``name=?`` (Cyrillic SQLite is case-sensitive).

    Does not reconstruct PascalCase from a fully lowercased identifier
    (``вводостатков`` will not match ``ВводОстатков``). First-letter upper of
    mixed input (``вводОстатков`` → ``ВводОстатков``) is the useful extra probe.
    """
    t = (name or "").strip()
    if not t:
        return []
    forms: list[str] = [t]
    if t[0].islower():
        forms.append(t[0].upper() + t[1:])
    lower = t.lower()
    if lower not in forms:
        forms.append(lower)
    upper = t.upper()
    if upper not in forms:
        forms.append(upper)
    return list(dict.fromkeys(forms))[: max(1, int(max_n))]



def load_path_index(conn: sqlite3.Connection, entity_id: int) -> dict[str, tuple[int, str]]:
    """path -> (id, content_hash). Prefer lookup_paths for streaming parse."""
    rows = conn.execute(
        "SELECT id, path, content_hash FROM objects WHERE entity_id=?",
        (entity_id,),
    ).fetchall()
    return {r["path"]: (int(r["id"]), r["content_hash"] or "") for r in rows}


def lookup_paths(
    conn: sqlite3.Connection,
    entity_id: int,
    paths: list[str],
) -> dict[str, tuple[int, str]]:
    """Batch path -> (id, content_hash) without loading the whole entity index."""
    if not paths:
        return {}
    out: dict[str, tuple[int, str]] = {}
    # SQLite variable limit is typically 999 — chunk IN lists
    chunk = 400
    for i in range(0, len(paths), chunk):
        part = paths[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT id, path, content_hash FROM objects
            WHERE entity_id=? AND path IN ({placeholders})
            """,
            (entity_id, *part),
        ).fetchall()
        for r in rows:
            out[r["path"]] = (int(r["id"]), r["content_hash"] or "")
    return out


def insert_new_objects(conn: sqlite3.Connection, entity_id: int, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    data = []
    for r in rows:
        props_json = json.dumps(r.get("props") or {}, ensure_ascii=False)
        ch = r.get("content_hash") or content_hash(r)
        data.append(
            (
                entity_id,
                r["path"],
                r["kind_ru"],
                r["kind"],
                r["name"],
                r.get("synonym") or "",
                r.get("comment") or "",
                r.get("belong") or "Own",
                r.get("base_object") or "",
                props_json,
                ch,
                int(r["parse_gen"]),
                0,
                r.get("source_rel") or "",
                (r.get("guid") or "").strip().lower(),
            )
        )
    conn.executemany(
        """
        INSERT INTO objects(
          entity_id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
          props_json, content_hash, parse_gen, embed_done, source_rel, guid
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(entity_id, path) DO UPDATE SET
          kind_ru=excluded.kind_ru,
          kind=excluded.kind,
          name=excluded.name,
          synonym=excluded.synonym,
          comment=excluded.comment,
          belong=excluded.belong,
          base_object=excluded.base_object,
          props_json=excluded.props_json,
          content_hash=excluded.content_hash,
          parse_gen=excluded.parse_gen,
          embed_done=0,
          source_rel=excluded.source_rel,
          guid=excluded.guid
        """,
        data,
    )
    return len(data)


def update_changed_objects(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    data = []
    for r in rows:
        props_json = json.dumps(r.get("props") or {}, ensure_ascii=False)
        ch = r.get("content_hash") or content_hash(r)
        data.append(
            (
                r["kind_ru"],
                r["kind"],
                r["name"],
                r.get("synonym") or "",
                r.get("comment") or "",
                r.get("belong") or "Own",
                r.get("base_object") or "",
                props_json,
                ch,
                int(r["parse_gen"]),
                0,
                r.get("source_rel") or "",
                (r.get("guid") or "").strip().lower(),
                int(r["id"]),
            )
        )
    conn.executemany(
        """
        UPDATE objects SET
          kind_ru=?, kind=?, name=?, synonym=?, comment=?, belong=?, base_object=?,
          props_json=?, content_hash=?, parse_gen=?, embed_done=?, source_rel=?, guid=?
        WHERE id=?
        """,
        data,
    )
    return len(data)


def touch_unchanged_objects(
    conn: sqlite3.Connection,
    items: list[int] | list[tuple[int, str]],
    parse_gen: int,
) -> int:
    if not items:
        return 0
    first = items[0]
    if isinstance(first, tuple):
        conn.executemany(
            """
            UPDATE objects SET parse_gen=?,
              source_rel=CASE WHEN ? != '' THEN ? ELSE source_rel END
            WHERE id=?
            """,
            [(parse_gen, rel, rel, i) for i, rel in items],  # type: ignore[misc]
        )
    else:
        conn.executemany(
            "UPDATE objects SET parse_gen=? WHERE id=?",
            [(parse_gen, int(i)) for i in items],
        )
    return len(items)


def touch_objects_by_source_rels(
    conn: sqlite3.Connection,
    entity_id: int,
    rels: Iterable[str],
    parse_gen: int,
) -> int:
    """Bump parse_gen for objects whose source file was skipped (mtime+size match)."""
    rel_list = [r for r in rels if r]
    if not rel_list:
        return 0
    total = 0
    chunk = 400
    for i in range(0, len(rel_list), chunk):
        part = rel_list[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        cur = conn.execute(
            f"""
            UPDATE objects SET parse_gen=?
            WHERE entity_id=? AND source_rel IN ({placeholders})
            """,
            (parse_gen, entity_id, *part),
        )
        total += int(cur.rowcount or 0)
    return total


def touch_all_objects_parse_gen(
    conn: sqlite3.Connection, entity_id: int, parse_gen: int
) -> int:
    """Bump parse_gen for every object of the entity (all dump files skipped)."""
    cur = conn.execute(
        "UPDATE objects SET parse_gen=? WHERE entity_id=?",
        (parse_gen, entity_id),
    )
    return int(cur.rowcount or 0)


def _chunked_rels(rels: Iterable[str]) -> Iterable[list[str]]:
    rel_list = [r for r in rels if r]
    chunk = 400
    for i in range(0, len(rel_list), chunk):
        yield rel_list[i : i + chunk]


def delete_links_for_source_rels(
    conn: sqlite3.Connection,
    entity_id: int,
    rels: Iterable[str],
    *,
    link_types: tuple[str, ...] | None = None,
    exclude_types: tuple[str, ...] | None = None,
) -> int:
    """Delete outgoing links from objects that live in the given source files."""
    deleted = 0
    type_sql = ""
    type_params: list[Any] = []
    if link_types:
        ph = ",".join("?" * len(link_types))
        type_sql += f" AND link_type IN ({ph})"
        type_params.extend(link_types)
    if exclude_types:
        ph = ",".join("?" * len(exclude_types))
        type_sql += f" AND link_type NOT IN ({ph})"
        type_params.extend(exclude_types)
    for part in _chunked_rels(rels):
        placeholders = ",".join("?" * len(part))
        cur = conn.execute(
            f"""
            DELETE FROM links
            WHERE entity_id=?
              AND from_path IN (
                SELECT path FROM objects WHERE entity_id=? AND source_rel IN ({placeholders})
              )
              {type_sql}
            """,
            (entity_id, entity_id, *part, *type_params),
        )
        deleted += int(cur.rowcount or 0)
    return deleted


def delete_help_links_for_owners(
    conn: sqlite3.Connection, entity_id: int, owners: Iterable[str]
) -> int:
    owner_list = [o for o in owners if o]
    if not owner_list:
        return 0
    deleted = 0
    chunk = 400
    for i in range(0, len(owner_list), chunk):
        part = owner_list[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        cur = conn.execute(
            f"""
            DELETE FROM links
            WHERE entity_id=? AND link_type='help' AND from_path IN ({placeholders})
            """,
            (entity_id, *part),
        )
        deleted += int(cur.rowcount or 0)
    return deleted


def cleanup_orphan_links(conn: sqlite3.Connection, entity_id: int) -> int:
    """Drop links whose source object (or help/call target) no longer exists."""
    cur1 = conn.execute(
        """
        DELETE FROM links
        WHERE entity_id=?
          AND NOT EXISTS (
            SELECT 1 FROM objects o
            WHERE o.entity_id=? AND o.path = links.from_path
          )
        """,
        (entity_id, entity_id),
    )
    cur2 = conn.execute(
        """
        DELETE FROM links
        WHERE entity_id=? AND link_type='help'
          AND NOT EXISTS (
            SELECT 1 FROM objects o
            WHERE o.entity_id=? AND o.kind='Help'
              AND json_extract(o.props_json, '$.owner') = links.from_path
          )
        """,
        (entity_id, entity_id),
    )
    cur3 = conn.execute(
        """
        DELETE FROM links
        WHERE entity_id=? AND link_type='calls'
          AND NOT EXISTS (
            SELECT 1 FROM objects o
            WHERE o.entity_id=? AND o.kind IN ('Procedure','Function')
              AND o.path = links.to_ref
          )
        """,
        (entity_id, entity_id),
    )
    return (
        int(cur1.rowcount or 0)
        + int(cur2.rowcount or 0)
        + int(cur3.rowcount or 0)
    )


def _queue_zvec_deletes(conn: sqlite3.Connection, entity_id: int, ids: list[int]) -> None:
    from app.services.bsl_embed import zvec_doc_ids_for_object

    rows = [
        (entity_id, doc_id)
        for i in ids
        for doc_id in zvec_doc_ids_for_object(i)
    ]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO pending_zvec_deletes(entity_id, doc_id) VALUES (?,?)",
            rows,
        )


def delete_stale_objects(
    conn: sqlite3.Connection,
    entity_id: int,
    parse_gen: int,
    *,
    kinds_only: tuple[str, ...] | None = None,
    exclude_kinds: tuple[str, ...] | None = None,
) -> list[int]:
    """Delete objects not seen in this parse; queue their ids for zvec delete. Returns deleted ids."""
    sql = "SELECT id FROM objects WHERE entity_id=? AND parse_gen < ?"
    params: list[Any] = [entity_id, parse_gen]
    if kinds_only:
        placeholders = ",".join("?" * len(kinds_only))
        sql += f" AND kind IN ({placeholders})"
        params.extend(kinds_only)
    if exclude_kinds:
        placeholders = ",".join("?" * len(exclude_kinds))
        sql += f" AND kind NOT IN ({placeholders})"
        params.extend(exclude_kinds)
    rows = conn.execute(sql, params).fetchall()
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return []
    _queue_zvec_deletes(conn, entity_id, ids)
    conn.executemany("DELETE FROM objects WHERE id=?", [(i,) for i in ids])
    return ids


def list_pending_zvec_deletes(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT doc_id FROM pending_zvec_deletes WHERE entity_id=?",
        (entity_id,),
    ).fetchall()
    return [r["doc_id"] for r in rows]


def clear_pending_zvec_deletes(conn: sqlite3.Connection, entity_id: int) -> None:
    conn.execute("DELETE FROM pending_zvec_deletes WHERE entity_id=?", (entity_id,))


def delete_method_objects(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """Remove all Procedure/Function objects and queue zvec doc deletes."""
    rows = conn.execute(
        "SELECT id FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
        (entity_id,),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    return delete_objects_by_ids(conn, entity_id, ids)


def method_parent_path(path: str, props_json: str | None) -> str:
    """Resolve metadata parent path for a BSL method object."""
    try:
        props = json.loads(props_json or "{}")
    except Exception:
        props = {}
    parent = str(props.get("parent_path") or "").strip()
    if parent:
        return parent
    marker = ".Методы."
    p = path or ""
    idx = p.find(marker)
    return p[:idx] if idx > 0 else ""


def _parent_alive(parent: str, parents: set[str]) -> bool:
    """Жив ли родитель модуля метода.

    Обычные родители (``Справочники.Х``, ``ОбщиеМодули.Х``, …) должны быть
    в objects ровно. Вложенные ``….Формы.Х`` / ``….Команды.Х`` индексируются
    на этапе 6 (ManagedForm); для них достаточно точного совпадения или живого
    базового контейнера-владельца (префикс по сегментам).
    """
    if parent in parents:
        return True
    if ".Формы." not in parent and ".Команды." not in parent:
        return False
    segments = parent.split(".")
    for i in range(len(segments) - 1, 0, -1):
        if ".".join(segments[:i]) in parents:
            return True
    return False


def list_orphan_method_ids(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """BSL methods whose parent metadata object is missing (deleted from report)."""
    parents = {
        str(r["path"])
        for r in conn.execute(
            """
            SELECT path FROM objects
            WHERE entity_id=? AND kind NOT IN ('Procedure','Function')
            """,
            (entity_id,),
        ).fetchall()
    }
    # Топ-уровневые модули (ManagedApplicationModule и пр.) живут на корне
    # ``Конфигурация``, которого нет в objects (этап 4) — считаем его живым.
    parents.add("Конфигурация")
    rows = conn.execute(
        """
        SELECT id, path, props_json FROM objects
        WHERE entity_id=? AND kind IN ('Procedure','Function')
        """,
        (entity_id,),
    ).fetchall()
    orphan: list[int] = []
    for r in rows:
        parent = method_parent_path(str(r["path"] or ""), r["props_json"])
        if not parent or not _parent_alive(parent, parents):
            orphan.append(int(r["id"]))
    return orphan


def count_orphan_methods(conn: sqlite3.Connection, entity_id: int) -> int:
    return len(list_orphan_method_ids(conn, entity_id))


def delete_objects_by_ids(
    conn: sqlite3.Connection, entity_id: int, ids: list[int]
) -> list[int]:
    if not ids:
        return []
    _queue_zvec_deletes(conn, entity_id, ids)
    conn.executemany("DELETE FROM objects WHERE id=?", [(i,) for i in ids])
    return ids


def delete_orphan_method_objects(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """Remove BSL methods without a living parent metadata object."""
    return delete_objects_by_ids(conn, entity_id, list_orphan_method_ids(conn, entity_id))


def count_embed_pending(conn: sqlite3.Connection, entity_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM objects
        WHERE entity_id=? AND (
          embed_done=0
          OR (kind IN ('Procedure','Function') AND bsl_embed_gen < ?)
        )
        """,
        (entity_id, _entity_bsl_embed_gen(conn, entity_id)),
    ).fetchone()
    return int(row["c"] if row else 0)


def _entity_bsl_embed_gen(conn: sqlite3.Connection, entity_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(bsl_embed_gen, 0) AS g FROM entities WHERE id=?",
        (entity_id,),
    ).fetchone()
    return int(row["g"] if row else 0)


def iter_embed_pending_ids(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    batch: int = 2000,
    methods: bool | None = None,
):
    """Yield lists of pending object ids (stable id cursor).

    ``methods``: True — only Procedure/Function; False — only other kinds;
    None — all pending.
    """
    batch = max(1, min(int(batch), 10_000))
    last_id = 0
    gen = _entity_bsl_embed_gen(conn, entity_id)
    kind_sql = ""
    if methods is True:
        kind_sql = " AND kind IN ('Procedure','Function')"
    elif methods is False:
        kind_sql = " AND kind NOT IN ('Procedure','Function')"
    while True:
        rows = conn.execute(
            f"""
            SELECT id FROM objects
            WHERE entity_id=? AND id>?{kind_sql}
              AND (
                embed_done=0
                OR (kind IN ('Procedure','Function') AND bsl_embed_gen < ?)
              )
            ORDER BY id LIMIT ?
            """,
            (entity_id, last_id, gen, batch),
        ).fetchall()
        if not rows:
            return
        ids = [int(r["id"]) for r in rows]
        last_id = ids[-1]
        yield ids


def count_embed_pending_split(conn: sqlite3.Connection, entity_id: int) -> tuple[int, int]:
    """Return (meta_pending, method_pending)."""
    gen = _entity_bsl_embed_gen(conn, entity_id)
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN kind IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS methods,
          COALESCE(SUM(CASE WHEN kind NOT IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS meta
        FROM objects
        WHERE entity_id=? AND (
          embed_done=0
          OR (kind IN ('Procedure','Function') AND bsl_embed_gen < ?)
        )
        """,
        (entity_id, gen),
    ).fetchone()
    return int(row["meta"] if row else 0), int(row["methods"] if row else 0)


def count_objects_split(conn: sqlite3.Connection, entity_id: int) -> tuple[int, int]:
    """Return (meta_count, method_count) for all objects of an entity."""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN kind IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS methods,
          COALESCE(SUM(CASE WHEN kind NOT IN ('Procedure','Function') THEN 1 ELSE 0 END), 0) AS meta
        FROM objects
        WHERE entity_id=?
        """,
        (entity_id,),
    ).fetchone()
    return int(row["meta"] if row else 0), int(row["methods"] if row else 0)


def count_embed_progress(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    scope: str = "",
) -> tuple[int, int]:
    """Return (done, target) from SQLite flags — source of truth for resume.

    ``scope`` ``bsl`` / ``report`` limits the counters to methods or metadata.
    """
    meta_total, meth_total = count_objects_split(conn, entity_id)
    meta_pend, meth_pend = count_embed_pending_split(conn, entity_id)
    key = (scope or "").strip().lower()
    if key == "bsl":
        return max(0, meth_total - meth_pend), meth_total
    if key == "report":
        return max(0, meta_total - meta_pend), meta_total
    total = meta_total + meth_total
    done = (meta_total - meta_pend) + (meth_total - meth_pend)
    return max(0, done), total


def insert_links_batch(conn: sqlite3.Connection, entity_id: int, links: Iterable[tuple[str, str, str]]) -> int:
    data = [(entity_id, a, b, c) for a, b, c in links]
    if not data:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO links(entity_id, from_path, to_ref, link_type) VALUES (?,?,?,?)",
        data,
    )
    # OR IGNORE skips dupes; count actual inserts, not len(data)
    return conn.total_changes - before


def query_links_by_to_refs(
    conn: sqlite3.Connection,
    entity_id: int,
    to_refs: Iterable[str],
    link_type: str | None = None,
) -> list[dict[str, Any]]:
    """Входящие ссылки по каноническим to_ref (для find_usages / trace_impact).

    Force ``idx_links_to`` / ``idx_links_to_cover``: without INDEXED BY, SQLite often
    picks ``idx_links_unique`` on ``entity_id`` alone and scans every link of large
    configs (hundreds of thousands of rows → multi-second find_usages).
    """
    refs = sorted({r for r in to_refs if r})
    if not refs:
        return []
    chunk = 400
    out: list[dict[str, Any]] = []
    index_hint = (
        "idx_links_to_cover" if _index_exists(conn, "idx_links_to_cover") else "idx_links_to"
    )
    for i in range(0, len(refs), chunk):
        part = refs[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        sql = (
            "SELECT from_path, to_ref, link_type FROM links "
            f"INDEXED BY {index_hint} "
            "WHERE entity_id=? AND to_ref IN (" + placeholders + ")"
        )
        params: list[Any] = [entity_id, *part]
        if link_type:
            sql += " AND link_type=?"
            params.append(link_type)
        rows = conn.execute(sql, params).fetchall()
        out.extend(dict(r) for r in rows)
    return out


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def get_object(conn: sqlite3.Connection, entity_id: int, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM objects WHERE entity_id=? AND path=?",
        (entity_id, path),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["props"] = json.loads(d.pop("props_json") or "{}")
    return d


def comments_by_paths(
    conn: sqlite3.Connection,
    entity_id: int,
    paths: Iterable[str],
) -> dict[str, str]:
    """path → comment for a batch of object paths."""
    paths_list = [p for p in paths if p]
    if not paths_list:
        return {}
    out: dict[str, str] = {}
    chunk = 400
    for i in range(0, len(paths_list), chunk):
        part = paths_list[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        rows = conn.execute(
            f"SELECT path, comment FROM objects WHERE entity_id=? AND path IN ({placeholders})",
            (entity_id, *part),
        ).fetchall()
        for r in rows:
            out[r["path"]] = r["comment"] or ""
    return out


def _like_patterns(token: str) -> list[str]:
    """Case variants for SQLite LIKE (NOCASE does not fold Cyrillic)."""
    t = (token or "").strip()
    if not t:
        return []
    forms = {t, t.casefold(), t.lower(), t.upper()}
    if len(t) > 1:
        forms.add(t[0].upper() + t[1:])
        forms.add(t[0].upper() + t[1:].casefold())
        forms.add(t[0].lower() + t[1:])
    # Short stem so NL «подотчетным» LIKE-matches «Подотчетниками».
    if len(t) >= 6:
        stem = t[: max(6, len(t) - 2)]
        if stem != t:
            forms.update(
                {
                    stem,
                    stem.casefold(),
                    stem.lower(),
                    stem.upper(),
                    stem[0].upper() + stem[1:].casefold() if len(stem) > 1 else stem,
                }
            )
    return [f"%{f}%" for f in forms if f]


# Meta-tree prefix → preferred kind (English dump). Report-index may store
# Russian collection names as kind — we OR both when filtering.
_STEM_PREFIX_KIND: dict[str, tuple[str, ...]] = {
    "Документы": ("Document", "Документы"),
    "Справочники": ("Catalog", "Справочники"),
    "Отчеты": ("Report", "Отчеты"),
    "Обработки": ("DataProcessor", "Обработки"),
    "РегистрыСведений": ("InformationRegister", "РегистрыСведений"),
    "РегистрыНакопления": ("AccumulationRegister", "РегистрыНакопления"),
    "ОбщиеМодули": ("CommonModule", "ОбщиеМодули"),
    "Роли": ("Role", "Роли"),
    "Константы": ("Constant", "Константы"),
    "ФункциональныеОпции": ("FunctionalOption", "ФункциональныеОпции"),
    "HTTPСервисы": ("HTTPService", "HTTPСервисы"),
    "РегламентныеЗадания": ("ScheduledJob", "РегламентныеЗадания"),
    "ОбщиеКартинки": ("CommonPicture", "ОбщиеКартинки"),
}


# Minimum stem length for leading-prefix recall lanes (U41 / P4).
# Gates prefix-lane only — not whether mid-LIKE is allowed under a narrow object.
NAME_PREFIX_MIN_STEM = 5


def search_by_name_prefixes(
    conn: sqlite3.Connection,
    entity_id: int,
    prefixes: list[str],
    *,
    path_prefix: str | None = None,
    include_borrowed: bool = True,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """U41: root name-prefix under a collection via path index.

    ``path LIKE 'Справочники.Stem%'`` (leading constant) uses ``idx_objects_path``.
    Do NOT use ``name LIKE`` / ``INDEXED BY idx_objects_name``: SQLite plans that
    as ``SEARCH entity_id=?`` and scans ~540k rows (~1–2s/stem on ERP Docker) —
    that was the marshrutnye-karty latency regression after P1–P3.
    """
    uniq = [
        p.strip()
        for p in dict.fromkeys(prefixes)
        if (p or "").strip() and len(p.strip()) >= NAME_PREFIX_MIN_STEM
    ]
    if not uniq:
        return []
    lim = max(1, min(int(limit), 100))
    pref = (path_prefix or "").strip().rstrip(".")
    if not pref:
        # Unscoped name-prefix is a full-table tax on ERP — refuse.
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in uniq:
        if len(out) >= lim:
            break
        stem = raw.strip()
        form = stem[0].upper() + stem[1:] if stem else stem
        # Collection.Stem… roots only (depth-2).
        path_like = f"{pref}.{form}%"
        nest_like = f"{pref}.%.%"
        sql = """
            SELECT path, kind, belong, name, synonym,
                   '' AS comment, NULL AS fill_checking
            FROM objects INDEXED BY idx_objects_path
            WHERE entity_id=?
              AND path LIKE ?
              AND path NOT LIKE ?
        """
        params: list[Any] = [entity_id, path_like, nest_like]
        if not include_borrowed:
            sql += " AND (belong='Own' OR belong IS NULL OR belong='')"
        sql += " LIMIT ?"
        params.append(min(lim, 12))
        for r in conn.execute(sql, params).fetchall():
            p = str(r["path"] or "")
            if not p or p in seen:
                continue
            if p.count(".") != 1:
                continue
            seen.add(p)
            out.append(
                {
                    "path": r["path"],
                    "kind": r["kind"],
                    "belong": r["belong"],
                    "name": r["name"],
                    "synonym": r["synonym"],
                    "comment": r["comment"] or "",
                    "fill_checking": r["fill_checking"],
                    "score": 1.0,
                }
            )
            if len(out) >= lim:
                break
    return out[:lim]


def search_by_stem_roots(
    conn: sqlite3.Connection,
    entity_id: int,
    stem: str,
    *,
    path_prefix: str,
    include_borrowed: bool = True,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Fast intent-stem recall: roots only, name/path LIKE, optional kind.

    U41: try indexable ``name LIKE 'Stem%'`` first; mid-string ``%Stem%`` under
    ``path BETWEEN`` only if the prefix lane is empty (mid-name hits like
    ``ОрдерНаОтражениеПересортицы``).

    Avoids synonym/comment ORs and ``ORDER BY length(path)`` over whole trees
    (those make ``search_by_tokens`` ~10s under ``Документы.*`` on ERP).

    Special case ``Отчеты.*``: many ERP dumps store only nested forms/layouts/
    Help under reports (no ``kind=Report`` root row). Match children, then
    collapse to ``Отчеты.<Name>`` so reconcile/report intents still work.
    """
    stem = (stem or "").strip()
    pref = (path_prefix or "").strip().rstrip(".")
    if not stem or not pref:
        return []
    lim = max(1, min(int(limit), 50))
    head = pref.split(".", 1)[0]
    report_tree = head in {"Отчеты", "Reports"}
    kinds = () if report_tree else _STEM_PREFIX_KIND.get(head, ())
    # CamelCase stems from ranking packs — keep 2–3 case forms only.
    forms = {stem, stem[0].upper() + stem[1:] if stem else stem}
    if len(stem) > 1:
        forms.add(stem[0].lower() + stem[1:])

    def _row_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "path": r["path"],
            "kind": r["kind"],
            "belong": r["belong"],
            "name": r["name"],
            "synonym": r["synonym"],
            "comment": r["comment"],
            "fill_checking": r["fill_checking"],
            "score": 1.0,
        }

    # U41 lexical-first: name prefix via collection tree (no mid-string tax).
    if not report_tree and len(stem) >= NAME_PREFIX_MIN_STEM:
        prefixed = search_by_name_prefixes(
            conn,
            entity_id,
            list(forms),
            path_prefix=pref,
            include_borrowed=include_borrowed,
            limit=lim,
        )
        if kinds:
            kind_set = set(kinds)
            prefixed = [h for h in prefixed if h.get("kind") in kind_set]
        if prefixed:
            return prefixed[:lim]
        # Mid-string ``%stem%`` under Documents.* is the ERP multi-second tax.
        # Systemic rule (batch4 P3): NEVER mid-LIKE under Документы — prefix
        # lane only. Listing individual stems was incomplete (возврат/бюджет).
        stem_cf = stem.casefold()
        if head in {"Документы", "Documents"}:
            return []
        if len(stem) < 8 or stem_cf.startswith(
            (
                "коэффициент",
                "стоимость",
                "эффективн",
                "распределен",
                "накладн",
                "возврат",
                "категор",
                "стать",
                "бюджет",
                "детализац",
                "номенклатур",
            )
        ):
            return []

    likes = [f"%{f}%" for f in forms if f]
    sql = """
        SELECT path, kind, belong, name, synonym, comment,
               json_extract(props_json, '$.FillChecking') AS fill_checking
        FROM objects
        WHERE entity_id=?
          AND path BETWEEN ? AND ?
    """
    lo, hi = path_range_params(pref + ".")
    params: list[Any] = [entity_id, lo, hi]
    if not report_tree:
        # Roots: Документы.X — exclude Документы.X.Y…
        sql += " AND path NOT LIKE ?"
        params.append(pref + ".%.%")
    if kinds:
        placeholders = ",".join("?" * len(kinds))
        sql += f" AND kind IN ({placeholders})"
        params.extend(kinds)
    if not include_borrowed:
        sql += " AND (belong='Own' OR belong IS NULL OR belong='')"
    field_ors = []
    for like in likes:
        field_ors.append("(name LIKE ? OR path LIKE ?)")
        params.extend([like, like])
    sql += " AND (" + " OR ".join(field_ors) + ") LIMIT ?"
    # Over-fetch nested report hits before collapsing to roots.
    params.append(lim * 8 if report_tree else lim)
    rows = list(conn.execute(sql, params).fetchall())
    if not report_tree:
        return [_row_dict(r) for r in rows]

    # Collapse Отчеты.Foo.… / Help.Отчеты.Foo → Отчеты.Foo
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        path = str(r["path"] or "")
        parts = path.split(".")
        root = ""
        if len(parts) >= 2 and parts[0] in {"Отчеты", "Reports"}:
            root = f"{parts[0]}.{parts[1]}"
        elif (
            len(parts) >= 3
            and parts[0] == "Help"
            and parts[1] in {"Отчеты", "Reports"}
        ):
            root = f"{parts[1]}.{parts[2]}"
        if not root or root in seen:
            continue
        # Stem must still match the report name segment.
        if not any(f.casefold() in parts[1 if parts[0] != "Help" else 2].casefold() for f in forms if f):
            # Also allow match on full path already filtered by SQL LIKE.
            pass
        seen.add(root)
        name = root.rsplit(".", 1)[-1]
        out.append(
            {
                "path": root,
                "kind": "Report",
                "belong": r["belong"],
                "name": name,
                "synonym": r["synonym"] or "",
                "comment": "",
                "fill_checking": None,
                "score": 1.0,
            }
        )
        if len(out) >= lim:
            break
    return out


def search_by_exact_names(
    conn: sqlite3.Connection,
    entity_id: int,
    names: list[str],
    *,
    path_prefix: str | None = None,
    include_borrowed: bool = True,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Exact ``name`` / canonical path match — no comment LIKE, no tree scan.

    Used for document/report alias recall: ``ПриобретениеТоваровУслуг`` under
    ``Документы.*`` must not walk the Documents tree (~8s on ERP when the
    planner prefers ``path BETWEEN`` over ``name=``).
    """
    uniq = [n for n in dict.fromkeys(names) if (n or "").strip()]
    if not uniq:
        return []
    lim = max(1, min(int(limit), 100))
    pref = (path_prefix or "").strip().rstrip(".")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    remaining = list(uniq)

    def _append_row(row: sqlite3.Row) -> None:
        p = row["path"]
        if p in seen:
            return
        if not include_borrowed and row["belong"] not in (None, "", "Own"):
            return
        seen.add(p)
        out.append(
            {
                "path": row["path"],
                "kind": row["kind"],
                "belong": row["belong"],
                "name": row["name"],
                "synonym": row["synonym"],
                "comment": "",
                "fill_checking": None,
                "score": 1.0,
            }
        )

    # 1) Canonical roots: <prefix>.<Name> via UNIQUE(entity_id, path)
    if pref:
        still: list[str] = []
        for name in remaining:
            if len(out) >= lim:
                break
            path = f"{pref}.{name}"
            row = conn.execute(
                """
                SELECT path, kind, belong, name, synonym
                FROM objects
                WHERE entity_id=? AND path=?
                """,
                (entity_id, path),
            ).fetchone()
            if row:
                _append_row(row)
            else:
                still.append(name)
        remaining = still
        if not remaining or len(out) >= lim:
            return out[:lim]

    # 2) name equality via idx_objects_name — never combine with path BETWEEN
    # (that plan scans the whole prefix tree on ERP).
    # Several case variants: Cyrillic ``name=?`` is case-sensitive; extra
    # equality seeks are cheap. Still no unscoped LIKE.
    for name in remaining:
        if len(out) >= lim:
            break
        for variant in name_eq_variants(name):
            if len(out) >= lim:
                break
            sql = """
            SELECT path, kind, belong, name, synonym
            FROM objects INDEXED BY idx_objects_name
            WHERE entity_id=? AND name=?
        """
            params: list[Any] = [entity_id, variant]
            rows = list(conn.execute(sql, params).fetchall())
            for row in rows:
                if pref:
                    p = row["path"] or ""
                    if not (p == pref or p.startswith(pref + ".")):
                        continue
                _append_row(row)
                if len(out) >= lim:
                    break
    return out[:lim]


def search_by_tokens(
    conn: sqlite3.Connection,
    entity_id: int,
    query: str,
    *,
    kind: str | None = None,
    include_borrowed: bool = False,
    path_prefix: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """SQLite LIKE search by query tokens (name/synonym/comment/path)."""
    import re

    tokens = [t for t in re.split(r"\s+", (query or "").strip()) if t]
    lim = max(1, min(int(limit), 500))
    rows = _search_by_tokens_sql(
        conn,
        entity_id,
        tokens,
        kind=kind,
        include_borrowed=include_borrowed,
        path_prefix=path_prefix,
        limit=lim,
        match_all=True,
    )
    # Cyrillic LIKE is case-sensitive: casefolded ``вводостатков`` misses CamelCase
    # names. Fall back to OR recall + casefold AND filter so mid-token hits
    # (``подотчет`` inside ``ВводОстатковСПодотчетниками``) still enter the set.
    # Skip OR fallback when unscoped: each %token% is already a full-table scan.
    # Skip for Help: AND already collapses to one token; OR re-scans the Help tree.
    pref = (path_prefix or "").strip().rstrip(".")
    unscoped = not pref and not kind
    help_scope = (kind == "Help") or (pref == "Help" or pref.startswith("Help."))
    if tokens and len(tokens) > 1 and not rows and not unscoped and not help_scope:
        rows = _search_by_tokens_sql(
            conn,
            entity_id,
            tokens,
            kind=kind,
            include_borrowed=include_borrowed,
            path_prefix=path_prefix,
            limit=min(max(lim * 4, 100), 500),
            match_all=False,
        )
        rows = _filter_rows_token_casefold(rows, tokens)
    return _score_token_rows(rows, tokens, lim)


def _search_by_tokens_sql(
    conn: sqlite3.Connection,
    entity_id: int,
    tokens: list[str],
    *,
    kind: str | None,
    include_borrowed: bool,
    path_prefix: str | None,
    limit: int,
    match_all: bool,
) -> list[sqlite3.Row]:
    pref = (path_prefix or "").strip().rstrip(".")
    # Help HTML in comment makes LIKE on comment catastrophic on large dumps.
    # Unscoped full-entity scans also skip comment: 540k×comment LIKE is multi-second.
    help_scope = (kind == "Help") or (pref == "Help" or pref.startswith("Help."))
    unscoped = not pref and not kind
    # CamelCase / identifier tokens: never LIKE comment (alias recall under
    # Документы.* / Отчеты.* was ~7s on ERP from comment HTML).
    ident_tokens = bool(
        tokens
        and all(
            t and (not any(c.isspace() for c in t)) and all(c.isalnum() or c in "._" for c in t)
            for t in tokens
        )
    )
    light_fields = help_scope or unscoped or ident_tokens
    # Help rows store large HTML in comment — covering index + slim SELECT
    # avoids loading those blobs on every LIKE probe (~0.6s → tens of ms on ERP).
    if help_scope:
        sql = """
            SELECT path, 'Help' AS kind, 'Own' AS belong, name,
                   '' AS synonym, '' AS comment, NULL AS fill_checking
            FROM objects INDEXED BY idx_objects_help_cover
            WHERE entity_id=?
              AND kind='Help'
        """
    elif light_fields:
        sql = """
            SELECT path, kind, belong, name, synonym,
                   '' AS comment, NULL AS fill_checking
            FROM objects
            WHERE entity_id=?
        """
    else:
        sql = """
            SELECT path, kind, belong, name, synonym, comment,
                   json_extract(props_json, '$.FillChecking') AS fill_checking
            FROM objects
            WHERE entity_id=?
        """
    params: list[Any] = [entity_id]
    if pref:
        sql += " AND path BETWEEN ? AND ?"
        params.extend(path_range_params(pref))
    if kind and not help_scope:
        sql += " AND kind=?"
        params.append(kind)
    if not include_borrowed:
        sql += " AND (belong='Own' OR belong IS NULL OR belong='')"
    work_tokens = list(tokens)
    # Unscoped AND of several %token% clauses cannot use indexes — collapse to
    # the longest token (OR fallback in search_by_tokens still applies if empty).
    if unscoped and match_all and len(work_tokens) > 1:
        work_tokens = [
            sorted(work_tokens, key=lambda t: (-len(t), t.casefold()))[0]
        ]
    # Help: one content token — AND+OR of verbs doubles a ~0.6s path scan.
    if help_scope and match_all and len(work_tokens) > 1:
        work_tokens = [
            sorted(work_tokens, key=lambda t: (-len(t), t.casefold()))[0]
        ]
    if work_tokens:
        clauses = []
        for t in work_tokens:
            patterns = _like_patterns(t)
            if help_scope or ident_tokens:
                patterns = _like_patterns_compact(t)
            elif light_fields and len(patterns) > 3:
                patterns = patterns[:3]
            field_ors = []
            for like in patterns:
                if help_scope:
                    field_ors.append("(name LIKE ? OR path LIKE ?)")
                    params.extend([like, like])
                elif light_fields:
                    field_ors.append(
                        "(name LIKE ? OR synonym LIKE ? OR path LIKE ?)"
                    )
                    params.extend([like, like, like])
                else:
                    field_ors.append(
                        "(name LIKE ? OR synonym LIKE ? OR comment LIKE ? OR path LIKE ?)"
                    )
                    params.extend([like, like, like, like])
            if field_ors:
                clauses.append("(" + " OR ".join(field_ors) + ")")
        if clauses:
            joiner = " AND " if match_all else " OR "
            sql += " AND (" + joiner.join(clauses) + ")"
    # Do NOT ORDER BY length(path) here: on ERP Help/Docs it forces a full sort
    # of every LIKE hit before LIMIT (~1s). Score/trim in _score_token_rows.
    fetch_cap = max(1, min(int(limit) * 4, 500))
    sql += " LIMIT ?"
    params.append(fetch_cap)
    return list(conn.execute(sql, params).fetchall())


def _like_patterns_compact(token: str) -> list[str]:
    """2–3 LIKE forms for Help / hot paths (full _like_patterns is 6+)."""
    t = (token or "").strip()
    if not t:
        return []
    forms: list[str] = [t]
    if len(t) > 1:
        title = t[0].upper() + t[1:].casefold()
        if title not in forms:
            forms.append(title)
    if len(t) >= 6:
        stem = t[: max(6, len(t) - 2)]
        if stem != t:
            st = stem[0].upper() + stem[1:].casefold() if len(stem) > 1 else stem
            if st not in forms:
                forms.append(st)
    return [f"%{f}%" for f in forms]


def _filter_rows_token_casefold(rows: list[sqlite3.Row], tokens: list[str]) -> list[sqlite3.Row]:
    tok_cf = [t.casefold() for t in tokens if t]
    if not tok_cf:
        return rows
    out: list[sqlite3.Row] = []
    for r in rows:
        hay = (
            f"{r['name'] or ''} {r['synonym'] or ''} "
            f"{r['comment'] or ''} {r['path'] or ''}"
        ).casefold()
        ok = True
        for t in tok_cf:
            if t in hay:
                continue
            if len(t) >= 6:
                stem = t[: max(6, len(t) - 2)]
                if stem in hay:
                    continue
            ok = False
            break
        if ok:
            out.append(r)
    return out


def _token_stem(token: str) -> str:
    """Short stem used by _like_patterns (NL «подотчетным» → «подотчет»)."""
    t = (token or "").strip()
    if len(t) >= 6:
        return t[: max(6, len(t) - 2)]
    return t


def _score_token_rows(
    rows: list[sqlite3.Row],
    tokens: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        name = r["name"] or ""
        syn = r["synonym"] or ""
        path = r["path"] or ""
        comment = r["comment"] or ""
        score = 1.0
        blob = f"{name} {syn} {comment} {path}".casefold()
        for t in tokens:
            tl = t.casefold()
            stem = _token_stem(tl)
            name_cf = name.casefold()
            syn_cf = syn.casefold()
            com_cf = comment.casefold()
            path_cf = path.casefold()
            if name_cf == tl:
                score += 8
            elif tl in name_cf or tl in syn_cf:
                score += 4
            elif tl in com_cf:
                score += 3
            elif tl in path_cf:
                score += 1
            if tl in blob:
                score += 0.5
            # Stem fallback: LIKE matched «продажам» via «продаж», so the score
            # must reflect it (otherwise all stem-matched rows tie at base score).
            if stem and stem != tl:
                if stem in name_cf or stem in syn_cf:
                    score += 4
                elif stem in com_cf:
                    score += 3
                elif stem in path_cf:
                    score += 1
                if stem in blob:
                    score += 0.5
        out.append(
            {
                "path": path,
                "kind": r["kind"],
                "belong": r["belong"] or "Own",
                "name": name,
                "synonym": syn,
                "comment": comment,
                "fill_checking": r["fill_checking"],
                "score": score,
            }
        )
    out.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
    return out[: max(1, min(int(limit), 500))]


def search_under_prefix(
    conn: sqlite3.Connection,
    entity_id: int,
    path_prefix: str,
    query: str,
    *,
    kind: str | None = None,
    include_borrowed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """SQLite scoped search: objects under path_prefix matching query tokens."""
    pref = (path_prefix or "").strip().rstrip(".")
    if not pref:
        return []
    return search_by_tokens(
        conn,
        entity_id,
        query,
        kind=kind,
        include_borrowed=include_borrowed,
        path_prefix=pref,
        limit=limit,
    )


def list_structure_children(
    conn: sqlite3.Connection,
    entity_id: int,
    parent_path: str,
    *,
    limit: int = 800,
) -> list[dict[str, Any]]:
    """Attrs / tabular sections / dimensions / resources under a root object."""
    pref = (parent_path or "").strip().rstrip(".")
    if not pref:
        return []
    lim = max(1, min(int(limit), 2000))
    rows = conn.execute(
        """
        SELECT path, kind, name, synonym, comment, props_json
        FROM objects
        WHERE entity_id=?
          AND path BETWEEN ? AND ?
          AND kind IN (
            'Attribute', 'TabularSection', 'TabularSectionAttribute',
            'Dimension', 'Resource'
          )
        ORDER BY path
        LIMIT ?
        """,
        (entity_id, *path_range_params(pref), lim),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            props = json.loads(r["props_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            props = {}
        if not isinstance(props, dict):
            props = {}
        fc = props.get("FillChecking")
        out.append(
            {
                "path": r["path"],
                "kind": r["kind"],
                "name": r["name"],
                "synonym": r["synonym"] or "",
                "comment": r["comment"] or "",
                "props": props,
                "fill_checking": (str(fc).strip() if fc else "") or None,
            }
        )
    return out


def list_fill_checking_under(
    conn: sqlite3.Connection,
    entity_id: int,
    path_prefix: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Attributes under prefix with non-default FillChecking or oblig. comment."""
    pref = (path_prefix or "").strip().rstrip(".")
    if not pref:
        return []
    lim = max(1, min(int(limit), 1000))
    rows = conn.execute(
        """
        SELECT path, kind, name, synonym, comment,
               json_extract(props_json, '$.FillChecking') AS fill_checking
        FROM objects
        WHERE entity_id=?
          AND path BETWEEN ? AND ?
          AND kind IN ('Attribute', 'TabularSectionAttribute', 'Dimension', 'Resource')
          AND (
            (
              json_extract(props_json, '$.FillChecking') IS NOT NULL
              AND trim(json_extract(props_json, '$.FillChecking')) != ''
              AND json_extract(props_json, '$.FillChecking') != 'DontCheck'
            )
            OR lower(ifnull(comment, '')) LIKE '%обязат%'
            OR lower(ifnull(comment, '')) LIKE '%required%'
            OR lower(ifnull(comment, '')) LIKE '%должен%'
            OR lower(ifnull(comment, '')) LIKE '%необход%'
          )
        ORDER BY path
        LIMIT ?
        """,
        (entity_id, *path_range_params(pref), lim),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        fc = (r["fill_checking"] or "").strip() or "DontCheck"
        out.append(
            {
                "path": r["path"],
                "kind": r["kind"],
                "name": r["name"],
                "synonym": r["synonym"],
                "comment": r["comment"] or "",
                "fill_checking": fc,
            }
        )
    return out


def list_title_document_candidates(
    conn: sqlite3.Connection,
    entity_id: int,
    title_num: str,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Document roots whose name ends with ТитулN / _ТитулN (disambiguation)."""
    num = (title_num or "").strip()
    if not num.isdigit():
        return []
    suffix = f"Титул{num}"
    lim = max(1, min(int(limit), 100))
    rows = conn.execute(
        """
        SELECT path, kind, name, synonym, comment
        FROM objects
        WHERE entity_id=?
          AND kind = 'Document'
          AND (
            name = ?
            OR name LIKE ?
            OR name LIKE ?
          )
        ORDER BY name
        LIMIT ?
        """,
        (
            entity_id,
            suffix,
            f"%_{suffix}",
            f"%{suffix}",
            lim,
        ),
    ).fetchall()
    # Prefer exact suffix match ordering: …_ТитулN before …ТитулNSomething
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        name = r["name"] or ""
        if name.endswith(f"_{suffix}") or name == suffix:
            rank = 0
        elif name.endswith(suffix):
            rank = 1
        else:
            rank = 2
        scored.append(
            (
                rank,
                {
                    "path": r["path"],
                    "kind": r["kind"],
                    "name": name,
                    "synonym": r["synonym"] or "",
                    "comment": r["comment"] or "",
                },
            )
        )
    scored.sort(key=lambda t: (t[0], t[1]["name"]))
    return [item for _, item in scored]


def mark_embedded(
    conn: sqlite3.Connection,
    ids: list[int],
    *,
    bsl_embed_gen: int | None = None,
) -> None:
    if not ids:
        return
    if bsl_embed_gen is None:
        conn.executemany("UPDATE objects SET embed_done=1 WHERE id=?", [(i,) for i in ids])
        return
    gen = int(bsl_embed_gen)
    conn.executemany(
        "UPDATE objects SET embed_done=1, bsl_embed_gen=? WHERE id=?",
        [(gen, i) for i in ids],
    )


def reset_embed_flags(conn: sqlite3.Connection, entity_id: int) -> None:
    conn.execute("UPDATE objects SET embed_done=0 WHERE entity_id=?", (entity_id,))


def reset_method_embed_flags(conn: sqlite3.Connection, entity_id: int | None = None) -> int:
    """Queue Procedure/Function re-embed by bumping entity generation (no per-row UPDATE)."""
    if entity_id is None:
        conn.execute(
            "UPDATE entities SET bsl_embed_gen = COALESCE(bsl_embed_gen, 0) + 1"
        )
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM objects WHERE kind IN ('Procedure','Function')"
        ).fetchone()
        return int(row["c"] if row else 0)
    conn.execute(
        "UPDATE entities SET bsl_embed_gen = COALESCE(bsl_embed_gen, 0) + 1 WHERE id=?",
        (entity_id,),
    )
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM objects
        WHERE entity_id=? AND kind IN ('Procedure','Function')
        """,
        (entity_id,),
    ).fetchone()
    return int(row["c"] if row else 0)


def path_or_children_exist(
    conn: sqlite3.Connection,
    entity_id: int,
    path: str,
) -> bool:
    """True if ``path`` is an object or has children (idx_objects_path range)."""
    pref = (path or "").strip().rstrip(".")
    if not pref:
        return False
    lo, hi = path_range_params(pref + ".")
    row = conn.execute(
        """
        SELECT 1 FROM objects INDEXED BY idx_objects_path
        WHERE entity_id=? AND (path=? OR path BETWEEN ? AND ?)
        LIMIT 1
        """,
        (entity_id, pref, lo, hi),
    ).fetchone()
    return row is not None


def reset_embed_flags_for_kind(
    conn: sqlite3.Connection,
    entity_id: int,
    kind: str,
) -> int:
    """Gap 3: mark one kind (e.g. ManagedForm) for targeted re-embed."""
    k = (kind or "").strip()
    if not k:
        return 0
    cur = conn.execute(
        "UPDATE objects SET embed_done=0 WHERE entity_id=? AND kind=?",
        (entity_id, k),
    )
    return int(cur.rowcount or 0)


def _literal_like_forms(query: str) -> list[str]:
    """Case variants for Cyrillic-safe mid-string LIKE (escaped)."""
    q = (query or "").strip()
    if not q:
        return []
    forms = {q, q.casefold(), q.lower(), q.upper()}
    if len(q) > 1:
        forms.add(q[0].upper() + q[1:])
        forms.add(q[0].upper() + q[1:].casefold())
    out: list[str] = []
    for f in forms:
        if not f:
            continue
        esc = f.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        out.append(f"%{esc}%")
    return out


def _literal_prefix_like_forms(query: str) -> list[str]:
    """Leading-anchor ``Stem%`` forms for prefix-lane (P3); same case set as mid."""
    q = (query or "").strip()
    if not q or any(c.isspace() for c in q):
        return []
    forms = {q, q.casefold(), q.lower(), q.upper()}
    if len(q) > 1:
        forms.add(q[0].upper() + q[1:])
        forms.add(q[0].upper() + q[1:].casefold())
    out: list[str] = []
    for f in forms:
        if not f:
            continue
        esc = f.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        out.append(f"{esc}%")
    return out


def _rows_to_literal_hits(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "path": r["path"],
            "kind": r["kind"],
            "belong": r["belong"],
            "name": r["name"],
            "synonym": r["synonym"],
            "comment": r["comment"] or "",
            "score": 1.0,
        }
        for r in rows
    ]

def find_by_guid(
    conn: sqlite3.Connection,
    entity_id: int,
    guid: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Indexed lookup by ``objects.guid`` (base UUID, lowercase)."""
    from app.services.guid_lookup import normalize_guid

    base, full = normalize_guid(guid)
    if not base:
        return []
    lim = max(1, min(int(limit), 50))
    keys = list(dict.fromkeys([full, base]))
    ph = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""
        SELECT path, kind, kind_ru, name, synonym, source_rel, guid
        FROM objects
        WHERE entity_id=? AND guid IN ({ph}) AND guid != ''
        LIMIT ?
        """,
        (entity_id, *keys, lim),
    ).fetchall()
    return [
        {
            "path": r["path"],
            "kind": r["kind"],
            "kind_ru": r["kind_ru"],
            "name": r["name"],
            "synonym": r["synonym"] or "",
            "source_rel": r["source_rel"] or "",
            "guid": r["guid"] or "",
            "source": "index",
        }
        for r in rows
    ]


def search_literal(
    conn: sqlite3.Connection,
    entity_id: int,
    query: str,
    *,
    kind: str | None = None,
    include_borrowed: bool = False,
    path_prefix: str | None = None,
    exclude_methods: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Substring search for exact identifiers / error fragments (no FTS).

    Unscoped (no ``path_prefix``): never mid-string LIKE / comment LIKE —
    that is a full-table tax on large dumps. Identifiers use indexed
    ``name=`` / ``path=``. Phrases return empty (caller should hint
    ``path_prefix`` / ``semantic_search``).

    With ``path_prefix``: path-range (``INDEXED BY idx_objects_path``) then
    P3 prefix-lane (``name/synonym/path LIKE 'Stem%'``, stem ≥
    ``NAME_PREFIX_MIN_STEM``) and mid ``%Stem%`` fallback on name/path/synonym.
    Comment is never in LIKE — HTML/long comments dominate I/O.

    Collection-root prefixes (``Документы``, …) must be refused by the
    service layer (``path_prefix_too_broad``); this function still accepts
    any prefix string for low-level callers.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    lim = max(1, min(int(limit), 200))
    pref = (path_prefix or "").strip().rstrip(".")
    kind_f = (kind or "").strip() or None

    def _kind_sql(where: list[str], params: list[Any]) -> None:
        if kind_f:
            where.append("kind=?")
            params.append(kind_f)
        else:
            where.append("kind NOT IN ('Help')")
            if exclude_methods:
                where.append("kind NOT IN ('Procedure', 'Function')")
        if not include_borrowed:
            where.append("(belong IS NULL OR belong='' OR belong='Own')")

    if not pref:
        # Unscoped: indexed equality only. Leading-wildcard LIKE scans the
        # whole entity (comment blobs × hundreds of thousands of rows).
        if any(c.isspace() for c in q):
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _keep(r: sqlite3.Row) -> None:
            if len(out) >= lim:
                return
            p = str(r["path"] or "")
            if not p or p in seen:
                return
            k = str(r["kind"] or "")
            if kind_f:
                if k != kind_f:
                    return
            else:
                if k == "Help":
                    return
                if exclude_methods and k in {"Procedure", "Function"}:
                    return
            if not include_borrowed and r["belong"] not in (None, "", "Own"):
                return
            seen.add(p)
            out.append(
                {
                    "path": r["path"],
                    "kind": r["kind"],
                    "belong": r["belong"],
                    "name": r["name"],
                    "synonym": r["synonym"],
                    "comment": r["comment"] or "",
                    "score": 1.0,
                }
            )

        row = conn.execute(
            """
            SELECT path, kind, belong, name, synonym, comment
            FROM objects
            WHERE entity_id=? AND path=?
            """,
            (entity_id, q),
        ).fetchone()
        if row:
            _keep(row)
        base_where = ["entity_id=?"]
        base_params: list[Any] = [entity_id]
        _kind_sql(base_where, base_params)
        for variant in name_eq_variants(q):
            if len(out) >= lim:
                break
            sql = f"""
                SELECT path, kind, belong, name, synonym, comment
                FROM objects INDEXED BY idx_objects_name
                WHERE {" AND ".join(base_where)} AND name=?
                LIMIT ?
            """
            for r in conn.execute(
                sql, [*base_params, variant, lim]
            ).fetchall():
                _keep(r)
                if len(out) >= lim:
                    break
        return out

    where = ["entity_id=?"]
    params: list[Any] = [entity_id]
    _kind_sql(where, params)
    # Include the prefix object itself, not only descendants.
    where.append("(path = ? OR path BETWEEN ? AND ?)")
    lo, hi = path_range_params(pref + ".")
    params.extend([pref, lo, hi])
    scope_sql = " AND ".join(where)

    def _scoped_field_query(field_ors: list[str], like_params: list[Any]) -> list[dict[str, Any]]:
        # U41: always force path index — never idx_objects_name with name LIKE.
        sql = f"""
            SELECT path, kind, belong, name, synonym, comment
            FROM objects INDEXED BY idx_objects_path
            WHERE {scope_sql}
              AND ({" OR ".join(field_ors)})
            LIMIT ?
        """
        rows = conn.execute(sql, [*params, *like_params, lim]).fetchall()
        return _rows_to_literal_hits(rows)

    # P3/P4: prefix-lane (Stem%) only when stem is long enough; mid always allowed
    # under object scope (short stems included).
    prefix_likes = (
        _literal_prefix_like_forms(q)
        if len(q) >= NAME_PREFIX_MIN_STEM and not any(c.isspace() for c in q)
        else []
    )
    if prefix_likes:
        field_ors: list[str] = []
        like_params: list[Any] = []
        for like in prefix_likes:
            field_ors.append(
                "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
                "OR synonym LIKE ? ESCAPE '\\')"
            )
            like_params.extend([like, like, like])
        prefixed = _scoped_field_query(field_ors, like_params)
        if prefixed:
            return prefixed

    likes = _literal_like_forms(q)
    if not likes:
        return []
    field_ors = []
    like_params = []
    for like in likes:
        # No comment LIKE (P2): comment blobs dominate I/O under large trees.
        field_ors.append(
            "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
            "OR synonym LIKE ? ESCAPE '\\')"
        )
        like_params.extend([like, like, like])
    return _scoped_field_query(field_ors, like_params)


def search_managed_forms(
    conn: sqlite3.Connection,
    entity_id: int,
    query: str,
    *,
    path_prefix: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Gap 3: SQL search ManagedForm by name/path/synonym/comment/props_json.

    Works before re-embed (props already in SQLite). FTS/semantic benefit after
    ``reset_embed_flags_for_kind(..., ManagedForm)`` + resume reindex.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    lim = max(1, min(int(limit), 100))
    pref = (path_prefix or "").strip().rstrip(".")
    where = ["entity_id=?", "kind='ManagedForm'"]
    params: list[Any] = [entity_id]
    if pref:
        where.append("path BETWEEN ? AND ?")
        params.extend(path_range_params(pref + "."))
    likes = _literal_like_forms(q)
    field_ors: list[str] = []
    for like in likes:
        field_ors.append(
            "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
            "OR synonym LIKE ? ESCAPE '\\' OR comment LIKE ? ESCAPE '\\' "
            "OR props_json LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like, like, like])
    sql = f"""
        SELECT path, kind, belong, name, synonym, comment
        FROM objects
        WHERE {" AND ".join(where)}
          AND ({" OR ".join(field_ors)})
        LIMIT ?
    """
    params.append(lim)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "path": r["path"],
            "kind": r["kind"],
            "belong": r["belong"],
            "name": r["name"],
            "synonym": r["synonym"],
            "comment": r["comment"] or "",
            "score": 1.0,
        }
        for r in rows
    ]


def get_links(
    conn: sqlite3.Connection,
    entity_id: int,
    path: str,
    direction: str = "both",
) -> dict[str, list[dict[str, Any]]]:
    """Outgoing by ``from_path``; incoming by exact ``to_ref=path``.

    Do not ``LIKE '%Name'`` here — on ERP that scans hundreds of thousands of
    links. Callers that need singular/plural ref variants should use
    ``query_links_by_to_refs`` + ``link_ref_candidates`` (see ``search.get_links``).
    """
    out: dict[str, list[dict[str, Any]]] = {"outgoing": [], "incoming": []}
    if direction in ("out", "both", "outgoing"):
        index_from = (
            "idx_links_from" if _index_exists(conn, "idx_links_from") else None
        )
        indexed = f" INDEXED BY {index_from}" if index_from else ""
        rows = conn.execute(
            f"SELECT from_path, to_ref, link_type FROM links{indexed} "
            "WHERE entity_id=? AND from_path=?",
            (entity_id, path),
        ).fetchall()
        out["outgoing"] = [dict(r) for r in rows]
    if direction in ("in", "both", "incoming"):
        out["incoming"] = query_links_by_to_refs(conn, entity_id, [path])
    return out


def get_help_for_owner(
    conn: sqlite3.Connection,
    entity_id: int,
    owner_path: str,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Справка (kind=Help) по объекту-владельцу: ``Help.<owner>`` + разделы.

    Точный путь + range ``Help.<owner>.`` — чтобы не захватить ``Help.<ownerX>``.
    """
    prefix = f"Help.{owner_path}"
    lim = max(1, min(int(limit), 500))
    rows = conn.execute(
        """
        SELECT path, name, synonym, comment
        FROM objects
        WHERE entity_id=?
          AND kind = 'Help'
          AND (path = ? OR path BETWEEN ? AND ?)
        ORDER BY path
        LIMIT ?
        """,
        (
            entity_id,
            prefix,
            prefix + ".",
            prefix + ".\uffff",
            lim,
        ),
    ).fetchall()
    return [dict(r) for r in rows]


def embedding_text(obj: dict[str, Any]) -> str:
    """Legacy single-passage text (meta only). Prefer bsl_embed.passages_for_object."""
    from app.services.bsl_embed import meta_text

    return meta_text(obj)
