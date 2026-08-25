"""Query helpers for BSL method objects (Procedure/Function)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.repositories.objects import path_range_params


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_method_name_query(q: str) -> bool:
    """True for a single identifier (PascalCase / dotted), not an NL phrase."""
    s = (q or "").strip()
    if not s or any(c.isspace() for c in s):
        return False
    return all(c.isalnum() or c in "._" for c in s)


def list_code_modules(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Parents that have at least one Procedure/Function child.

    Requires ``q`` (≥2 chars). Candidates: indexed name prefix, then (only when
    ``kind`` is set) a mid-string LIKE inside that kind. Unscoped ``%q%`` over
    all non-method rows is a full table scan (~23s on a large dump for
    ``q=Себестоимость``, 2026-08-18) and is not used.
    """
    qn = (q or "").strip()
    if len(qn) < 2:
        return []

    lim = max(1, min(int(limit or 500), 5000))
    forms = {qn, qn.casefold(), qn.lower(), qn.upper()}
    if len(qn) > 1:
        forms.add(qn[0].upper() + qn[1:].casefold())
        forms.add(qn[0].upper() + qn[1:])
    forms = {f for f in forms if f}
    kind_f = (kind or "").strip() or None

    where = ["entity_id=?"]
    params: list[Any] = [entity_id]
    if kind_f:
        where.append("kind=?")
        params.append(kind_f)
    else:
        where.append("kind NOT IN ('Procedure', 'Function', 'Help')")
    base = " AND ".join(where)

    prefix_ors = " OR ".join(["(name >= ? AND name < ?)" for _ in forms])
    prefix_params: list[Any] = []
    for f in forms:
        prefix_params.extend([f, f + "\uffff"])
    indexed = " INDEXED BY idx_objects_kind_name" if kind_f else " INDEXED BY idx_objects_name"
    cand_rows = list(
        conn.execute(
            f"""
            SELECT path, kind, name, synonym
            FROM objects{indexed}
            WHERE {base}
              AND ({prefix_ors})
            LIMIT 2000
            """,
            (*params, *prefix_params),
        ).fetchall()
    )
    # Mid-string only inside a kind (CommonModule ~4k rows, tens of ms).
    # Unscoped %LIKE% on the whole object table is the 23s trap.
    if kind_f and len(cand_rows) < 2000:
        likes = [f"%{_like_escape(f)}%" for f in forms]
        like_ors = " OR ".join(
            ["(path LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')" for _ in likes]
        )
        like_params: list[Any] = []
        for like in likes:
            like_params.extend([like, like])
        seen = {r["path"] for r in cand_rows}
        extra = conn.execute(
            f"""
            SELECT path, kind, name, synonym
            FROM objects
            WHERE {base}
              AND ({like_ors})
            LIMIT 2000
            """,
            (*params, *like_params),
        ).fetchall()
        for r in extra:
            if r["path"] not in seen:
                seen.add(r["path"])
                cand_rows.append(r)
                if len(cand_rows) >= 2000:
                    break
    by_path = {r["path"]: dict(r) for r in cand_rows}
    parents = list(by_path.keys())
    if not parents:
        return []

    counts: dict[str, int] = {}
    for parent in parents:
        # Child range (parent + '.') — bare prefix leaks siblings
        # (ОбщегоНазначения → ОбщегоНазначенияБПО).
        lo, hi = path_range_params(parent + ".")
        r = conn.execute(
            """
            SELECT COUNT(*) AS methods_count
            FROM objects INDEXED BY idx_objects_path
            WHERE entity_id=?
              AND path BETWEEN ? AND ?
              AND kind IN ('Procedure', 'Function')
            """,
            (entity_id, lo, hi),
        ).fetchone()
        cnt = int(r["methods_count"] or 0) if r else 0
        if cnt > 0:
            counts[parent] = cnt
    parents = [p for p in parents if counts.get(p)]
    parents = sorted(
        parents,
        key=lambda p: (-counts.get(p, 0), p.lower()),
    )[:lim]

    out: list[dict[str, Any]] = []
    for parent in parents:
        meta = by_path.get(parent) or {
            "path": parent,
            "kind": "",
            "name": parent.rsplit(".", 1)[-1],
            "synonym": "",
        }
        if kind and meta.get("kind") != kind:
            continue
        out.append(
            {
                "path": parent,
                "kind": meta.get("kind") or "",
                "name": meta.get("name") or "",
                "methods_count": counts.get(parent, 0),
                "module_roles": [],
            }
        )
    return out


def _truthy_export(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value or "").strip().lower()
    return s in {"1", "true"}


def _row_to_list_item(r: sqlite3.Row) -> dict[str, Any]:
    keys = set(r.keys())
    props: dict[str, Any] = {}
    if "props_json" in keys:
        try:
            props = json.loads(r["props_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            props = {}
    parent = props.get("parent_path") or ""
    if not parent and "parent_path" in keys:
        parent = str(r["parent_path"] or "")
    export = props.get("export")
    if export is None and "export" in keys:
        export = r["export"]
    signature = props.get("signature") or ""
    if not signature and "signature" in keys:
        signature = str(r["signature"] or "")
    if not signature:
        signature = str(r["synonym"] or "")
    role = props.get("module_role") or ""
    if not role and "module_role" in keys:
        role = str(r["module_role"] or "")
    doc = r["comment"] or "" if "comment" in keys else ""
    preview = doc.replace("\n", " ").strip()
    if len(preview) > 200:
        preview = preview[:197] + "..."
    return {
        "path": r["path"],
        "parent_path": parent,
        "name": r["name"] or "",
        "kind": r["kind"],
        "export": _truthy_export(export),
        "signature": signature,
        "doc_preview": preview,
        "module_role": role,
    }


def list_methods(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    parent_path: str | None = None,
    q: str | None = None,
    export_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List Procedure/Function rows with filters pushed into SQL + LIMIT.

    Name equality/prefix use ``idx_objects_kind_name`` with range seeks
    (``name >= prefix AND name < prefix||\\uffff``). ``LIKE 'x%'`` under
    ``INDEXED BY idx_objects_name`` only seeks ``entity_id`` and scans the
    whole config — hundreds of ms on ERP/BP.

    Natural-language phrases skip mid-string LIKE (name/path/synonym/comment):
    that path is multi-second on ERP and belongs to fuzzy/zvec in ``find_methods``.
    """
    lim = max(1, min(int(limit or 50), 500))
    parent_f = (parent_path or "").strip().rstrip(".")
    query = (q or "").strip()

    where = ["entity_id = ?"]
    params: list[Any] = [entity_id]

    if parent_f:
        # Child range (parent + '.') — bare prefix leaks siblings
        # (ОбщиеМодули.Мод → ОбщиеМодули.МодБПО, Документы.Заказ → ЗаказКлиента).
        # Form methods under a document stay in: they start with parent + '.'.
        where.append("path BETWEEN ? AND ?")
        params.extend(path_range_params(parent_f + "."))

    if export_only:
        where.append(
            "LOWER(COALESCE(json_extract(props_json, '$.export'), '0')) IN ('1', 'true')"
        )

    base_where = " AND ".join(where)
    select_cols = "path, kind, name, synonym, comment, props_json"

    def _fetch_kinds(extra_sql: str, extra_params: list[Any], fetch_limit: int) -> list[dict[str, Any]]:
        """Exact/prefix seeks per kind via idx_objects_kind_name."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        per = max(fetch_limit, 1)
        for kind in ("Procedure", "Function"):
            sql = (
                f"SELECT {select_cols} FROM objects INDEXED BY idx_objects_kind_name "
                f"WHERE {base_where} AND kind=?{extra_sql} LIMIT ?"
            )
            rows = conn.execute(
                sql, [*params, kind, *extra_params, per]
            ).fetchall()
            for r in rows:
                item = _row_to_list_item(r)
                if item["path"] in seen:
                    continue
                seen.add(item["path"])
                out.append(item)
                if len(out) >= fetch_limit:
                    return out[:fetch_limit]
        return out[:fetch_limit]

    def _fetch_scan(extra_sql: str, extra_params: list[Any], fetch_limit: int) -> list[dict[str, Any]]:
        indexed = " INDEXED BY idx_objects_path" if parent_f else ""
        sql = (
            f"SELECT {select_cols} FROM objects{indexed} "
            f"WHERE {base_where} AND kind IN ('Procedure', 'Function'){extra_sql} "
            f"LIMIT ?"
        )
        rows = conn.execute(sql, [*params, *extra_params, fetch_limit]).fetchall()
        return [_row_to_list_item(r) for r in rows]

    if not query:
        return _fetch_scan("", [], lim)

    if parent_f:
        # Path index + name equality/prefix. INDEXED BY idx_objects_kind_name
        # plus path BETWEEN scans all prefix names in the config (~2.7s on ERP
        # for «Заполнить» under one form, 2026-08-18).
        out = _fetch_scan(" AND name = ?", [query], lim)
        if len(out) >= lim:
            return out[:lim]
        seen = {item["path"] for item in out}
        hi = query + "\uffff"
        for item in _fetch_scan(" AND name >= ? AND name < ?", [query, hi], lim):
            if item["path"] in seen:
                continue
            out.append(item)
            seen.add(item["path"])
            if len(out) >= lim:
                return out[:lim]
        return out[:lim]

    out = _fetch_kinds(" AND name = ?", [query], lim)
    if len(out) >= lim:
        return out[:lim]

    seen = {item["path"] for item in out}
    # Prefix via index range (not LIKE): seeks (entity_id, kind, name).
    hi = query + "\uffff"
    for item in _fetch_kinds(" AND name >= ? AND name < ?", [query, hi], lim):
        if item["path"] in seen:
            continue
        out.append(item)
        seen.add(item["path"])
        if len(out) >= lim:
            return out[:lim]

    # Identifier: stop after exact+prefix. Never mid-string LIKE on ERP
    # (comment/body blobs × ~400k methods ≈ tens of seconds). Empty → fuzzy
    # zvec in ``find_methods`` (Gap 1). NL phrases already return above.
    return out[:lim]


def list_methods_literal(
    conn: sqlite3.Connection,
    entity_id: int,
    query: str,
    *,
    parent_path: str | None = None,
    export_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Mid-string LIKE on method name/path/signature (synonym). No fuzzy.

    Prefer ``parent_path`` on large dumps. Does not scan comment/body
    (use ``find_code_references`` for body substrings / error text).
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    lim = max(1, min(int(limit or 50), 200))
    parent_f = (parent_path or "").strip().rstrip(".")
    forms = {q, q.casefold(), q.lower(), q.upper()}
    if len(q) > 1:
        forms.add(q[0].upper() + q[1:])
        forms.add(q[0].upper() + q[1:].casefold())
    likes = [f"%{_like_escape(f)}%" for f in forms if f]
    if not likes:
        return []

    where = ["entity_id = ?", "kind IN ('Procedure', 'Function')"]
    params: list[Any] = [entity_id]
    if parent_f:
        where.append("path BETWEEN ? AND ?")
        params.extend(path_range_params(parent_f + "."))
    if export_only:
        where.append(
            "LOWER(COALESCE(json_extract(props_json, '$.export'), '0')) IN ('1', 'true')"
        )
    like_ors = " OR ".join(
        [
            "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
            "OR synonym LIKE ? ESCAPE '\\')"
            for _ in likes
        ]
    )
    for like in likes:
        params.extend([like, like, like])
    # json_extract — not full props_json (BSL bodies make the range scan I/O-bound).
    indexed = " INDEXED BY idx_objects_path" if parent_f else ""
    sql = (
        f"SELECT path, kind, name, synonym, comment, "
        f"json_extract(props_json, '$.parent_path') AS parent_path, "
        f"json_extract(props_json, '$.export') AS export, "
        f"json_extract(props_json, '$.signature') AS signature, "
        f"json_extract(props_json, '$.module_role') AS module_role "
        f"FROM objects{indexed} "
        f"WHERE {' AND '.join(where)} AND ({like_ors}) LIMIT ?"
    )
    params.append(lim)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_list_item(r) for r in rows]


def _row_to_method(r: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
    kind = r["kind"]
    if kind not in {"Procedure", "Function"}:
        return None
    try:
        props = json.loads(r["props_json"] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError):
        props = {}
    source_rel = ""
    try:
        source_rel = (r["source_rel"] or "") if "source_rel" in r.keys() else ""
    except (IndexError, KeyError, TypeError):
        source_rel = ""
    if not source_rel:
        source_rel = props.get("source_file") or ""
    return {
        "path": r["path"],
        "parent_path": props.get("parent_path") or "",
        "name": r["name"] or "",
        "kind": kind,
        "export": bool(props.get("export")),
        "signature": props.get("signature") or r["synonym"] or "",
        "doc": r["comment"] or "",
        "body": props.get("body") or "",
        "load_mode": props.get("load_mode") or "signatures",
        "module_role": props.get("module_role") or "",
        "source_file": props.get("source_file") or "",
        "source_rel": source_rel,
        "line": props.get("line"),
    }


def get_method(conn: sqlite3.Connection, entity_id: int, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT path, kind, name, synonym, comment, props_json, source_rel
        FROM objects
        WHERE entity_id=? AND path=?
        """,
        (entity_id, path),
    ).fetchone()
    if not row:
        return None
    return _row_to_method(row)


def iter_methods_under(
    conn: sqlite3.Connection,
    entity_id: int,
    parent_path: str,
) -> list[dict[str, Any]]:
    """Procedure/Function rows whose path is under the object (path-prefix seek).

    Range is ``parent + '.'`` so sibling names that extend the parent
    (``…МодБПО`` next to ``…Мод``) do not leak in.
    """
    parent = (parent_path or "").strip().rstrip(".")
    if not parent:
        return []
    rows = conn.execute(
        """
        SELECT path, kind, name, synonym, comment, props_json
        FROM objects INDEXED BY idx_objects_path
        WHERE entity_id=?
          AND path BETWEEN ? AND ?
          AND kind IN ('Procedure', 'Function')
        """,
        (entity_id, *path_range_params(parent + ".")),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        method = _row_to_method(r)
        if method:
            out.append(method)
    out.sort(key=lambda m: (m.get("path") or "", m.get("name") or ""))
    return out


def iter_methods_under_props_matching(
    conn: sqlite3.Connection,
    entity_id: int,
    parent_path: str,
    needles: list[str],
    *,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Methods under ``parent`` whose props_json contains any needle (SQL prefilter).

    Avoids loading every method body under a large document for
    ``get_required_fields`` (was ~55s on ERP ПриобретениеТоваровУслуг).
    """
    parent = (parent_path or "").strip().rstrip(".")
    uniq = [n for n in dict.fromkeys(needles) if n]
    if not parent or not uniq:
        return []
    lim = max(1, min(int(limit), 200))
    ors = " OR ".join(["instr(props_json, ?) > 0"] * len(uniq))
    rows = conn.execute(
        f"""
        SELECT path, kind, name, synonym, comment, props_json
        FROM objects INDEXED BY idx_objects_path
        WHERE entity_id=?
          AND path BETWEEN ? AND ?
          AND kind IN ('Procedure', 'Function')
          AND ({ors})
        LIMIT ?
        """,
        (entity_id, *path_range_params(parent + "."), *uniq, lim),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        method = _row_to_method(r)
        if method:
            out.append(method)
    out.sort(key=lambda m: (m.get("path") or "", m.get("name") or ""))
    return out


def get_methods_named_under(
    conn: sqlite3.Connection,
    entity_id: int,
    parent_path: str,
    names: list[str],
) -> list[dict[str, Any]]:
    """Load Procedure/Function rows by exact name scoped to parent (no full scan)."""
    parent = (parent_path or "").strip()
    uniq = [n for n in dict.fromkeys(names) if n]
    if not parent or not uniq:
        return []
    ph = ",".join("?" * len(uniq))
    rows = conn.execute(
        f"""
        SELECT path, kind, name, synonym, comment, props_json
        FROM objects
        WHERE entity_id=?
          AND kind IN ('Procedure', 'Function')
          AND name IN ({ph})
          AND (
            json_extract(props_json, '$.parent_path') = ?
            OR path BETWEEN ? AND ?
          )
        """,
        (entity_id, *uniq, parent, *path_range_params(parent + ".Методы")),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        method = _row_to_method(r)
        if method:
            out.append(method)
    # Stable order by requested names
    order = {n: i for i, n in enumerate(uniq)}
    out.sort(key=lambda m: (order.get(m.get("name") or "", 999), m.get("path") or ""))
    return out


def get_methods_by_paths(
    conn: sqlite3.Connection,
    entity_id: int,
    paths: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-load Procedure/Function rows keyed by path (order of paths ignored).

    Always ``INDEXED BY idx_objects_path``: bare ``path IN (…)`` on ERP plans as
    a full entity scan and loads ``props_json`` (BSL bodies) for ~40–90s on a
    few dozen paths (find_methods fuzzy+parent_path rank bottleneck).
    """
    uniq = [p for p in dict.fromkeys(paths) if p]
    if not uniq:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk = 400
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        ph = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT path, kind, name, synonym, comment, props_json
            FROM objects INDEXED BY idx_objects_path
            WHERE entity_id=? AND path IN ({ph}) AND kind IN ('Procedure','Function')
            """,
            (entity_id, *part),
        ).fetchall()
        for r in rows:
            method = _row_to_method(r)
            if method:
                out[method["path"]] = method
    return out


def get_module_structure(
    conn: sqlite3.Connection,
    entity_id: int,
    parent_path: str,
) -> dict[str, Any] | None:
    """Структура модуля: методы (line/region/signature), регионы, экспорты.

    Methods live under ``parent + '.'`` (not bare ``parent`` prefix): otherwise
    ``ОбщиеМодули.ОбщегоНазначения`` would also pull ``…ОбщегоНазначенияБПО…``.
    Select json_extract fields only — full ``props_json`` includes BSL bodies and
    makes a full-config scan multi-second on ERP.
    """
    parent = (parent_path or "").strip()
    if not parent:
        return None
    # Child path range: parent + '.' … parent + '.' + max — no sibling leak.
    lo, hi = path_range_params(parent + ".")
    rows = conn.execute(
        """
        SELECT path, kind, name, synonym,
               json_extract(props_json, '$.parent_path') AS parent_path,
               json_extract(props_json, '$.module_role') AS module_role,
               json_extract(props_json, '$.export') AS export,
               json_extract(props_json, '$.line') AS line,
               json_extract(props_json, '$.region') AS region,
               json_extract(props_json, '$.region_line') AS region_line,
               json_extract(props_json, '$.signature') AS signature,
               json_extract(props_json, '$.load_mode') AS load_mode
        FROM objects INDEXED BY idx_objects_path
        WHERE entity_id=?
          AND path BETWEEN ? AND ?
          AND kind IN ('Procedure', 'Function')
        """,
        (entity_id, lo, hi),
    ).fetchall()
    methods: list[dict[str, Any]] = []
    regions: dict[str, dict[str, Any]] = {}
    exported: list[str] = []
    role = ""
    for r in rows:
        # Prefer path-range children; keep parent_path check for odd nests.
        row_parent = str(r["parent_path"] or "")
        if row_parent and row_parent != parent:
            continue
        kind = r["kind"] or ""
        if kind not in {"Procedure", "Function"}:
            continue
        role = str(r["module_role"] or "") or role
        name = r["name"] or ""
        export_raw = r["export"]
        export = bool(export_raw) and str(export_raw).lower() not in ("0", "false", "")
        line = r["line"]
        load_mode = str(r["load_mode"] or "")
        region = str(r["region"] or "") if load_mode == "full" else ""
        signature = r["signature"] or r["synonym"] or ""
        methods.append(
            {
                "name": name,
                "kind": kind,
                "export": export,
                "line": line,
                "region": region,
                "signature": signature,
            }
        )
        if region:
            reg = regions.setdefault(
                region,
                {"name": region, "line": r["region_line"] or 0, "methods_count": 0},
            )
            reg["methods_count"] += 1
        if export:
            exported.append(name)

    if not methods:
        return None
    methods.sort(key=lambda m: (m["line"] or 0, m["name"]))
    exported.sort(key=lambda n: n.lower())
    return {
        "module_path": parent,
        "role": role,
        "methods": methods,
        "regions": sorted(regions.values(), key=lambda x: (x["line"] or 0, x["name"])),
        "exported": exported,
        "total_methods": len(methods),
    }
