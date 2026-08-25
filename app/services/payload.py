"""Compact MCP payloads: owner aggregation, pagination, dossier structure."""

from __future__ import annotations

import json
from typing import Any

SUMMARY_EXCLUDE_PREFIXES = ("ОпределяемыеТипы.", "DefinedTypes.")
DEFAULT_PAGE = 50
DEFAULT_USAGE_TOP_N = 20
MAX_ATTRS = 200
MAX_TABULAR = 40
MAX_COLUMNS = 40
MAX_MOVEMENTS = 40
SAMPLE_PATHS = 3

_STRUCTURE_KINDS = frozenset(
    {"Attribute", "TabularSection", "TabularSectionAttribute", "Dimension", "Resource"}
)


def owner_path(path: str) -> str:
    """``Документы.X.Реквизиты.Y`` → ``Документы.X``."""
    parts = [p for p in (path or "").split(".") if p]
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return path or ""


def is_summary_excluded(path: str) -> bool:
    p = path or ""
    return p.startswith(SUMMARY_EXCLUDE_PREFIXES)


def object_card(obj: dict[str, Any], *, include_comment: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": obj.get("path"),
        "kind": obj.get("kind"),
        "name": obj.get("name"),
        "synonym": obj.get("synonym") or "",
    }
    if include_comment:
        cmt = obj.get("comment") or ""
        if cmt:
            out["comment"] = cmt
    return out


def format_type(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, list):
        parts = [str(x).strip() for x in raw if str(x).strip()]
        extra = "…" if len(parts) > 5 else ""
        return ", ".join(parts[:5]) + extra
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                return format_type(json.loads(s))
            except json.JSONDecodeError:
                pass
        return s[:160]
    return str(raw)[:160]


def _type_from_props(props: Any) -> str:
    if not isinstance(props, dict):
        return ""
    return format_type(props.get("Type"))


def paginate(
    items: list[Any],
    *,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    total = len(items)
    lim = max(1, int(limit))
    off = max(0, int(offset))
    page = items[off : off + lim]
    return {
        "items": page,
        "total": total,
        "limit": lim,
        "offset": off,
        "truncated": total > off + lim,
    }


def aggregate_paths(
    rows: list[dict[str, Any]],
    *,
    path_key: str = "from_path",
    exclude_defined_types: bool = True,
    limit: int = DEFAULT_USAGE_TOP_N,
    offset: int = 0,
) -> dict[str, Any]:
    """Group full paths to owner objects + counts. Sorted by count desc."""
    grouped: dict[str, dict[str, Any]] = {}
    skipped = 0
    for row in rows:
        path = str(row.get(path_key) or row.get("path") or "")
        if not path:
            continue
        if exclude_defined_types and is_summary_excluded(path):
            skipped += 1
            continue
        owner = owner_path(path)
        slot = grouped.get(owner)
        if slot is None:
            slot = {
                "path": owner,
                "count": 0,
                "link_types": {},
                "sample_paths": [],
            }
            grouped[owner] = slot
        slot["count"] += 1
        lt = str(row.get("link_type") or "")
        if lt:
            slot["link_types"][lt] = int(slot["link_types"].get(lt, 0)) + 1
        if path != owner and len(slot["sample_paths"]) < SAMPLE_PATHS:
            if path not in slot["sample_paths"]:
                slot["sample_paths"].append(path)

    owners = sorted(grouped.values(), key=lambda o: (-int(o["count"]), o["path"]))
    page = paginate(owners, limit=limit, offset=offset)
    items = []
    for o in page["items"]:
        item = {
            "path": o["path"],
            "count": o["count"],
            "link_types": o["link_types"],
        }
        if o["sample_paths"]:
            item["sample_paths"] = o["sample_paths"]
        items.append(item)
    return {
        "items": items,
        "total": page["total"],
        "limit": page["limit"],
        "offset": page["offset"],
        "truncated": page["truncated"],
        "counts": {
            "paths": len(rows),
            "owners": page["total"],
            "excluded_defined_types": skipped,
        },
    }


def compact_structure(
    parent_path: str,
    children: list[dict[str, Any]],
    outgoing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attrs (name+type), tabular sections (name+columns), movements — no XML props."""
    pref = (parent_path or "").strip().rstrip(".")
    pref_dots = pref.count(".")
    attrs: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    ts_map: dict[str, dict[str, Any]] = {}
    attrs_trunc = False
    ts_trunc = False

    prefix_attr = pref + ".Реквизиты."
    prefix_ts = pref + ".ТабличныеЧасти."
    prefix_dim = pref + ".Измерения."
    prefix_res = pref + ".Ресурсы."

    for row in children:
        kind = row.get("kind") or ""
        if kind not in _STRUCTURE_KINDS:
            continue
        path = str(row.get("path") or "")
        name = row.get("name") or path.rsplit(".", 1)[-1]
        entry = {"name": name}
        syn = row.get("synonym") or ""
        if syn and syn != name:
            entry["synonym"] = syn
        typ = _type_from_props(row.get("props"))
        if not typ:
            typ = format_type(row.get("type"))
        if typ:
            entry["type"] = typ
        fc = row.get("fill_checking")
        if not fc and isinstance(row.get("props"), dict):
            fc = row["props"].get("FillChecking")
        if fc and str(fc) not in ("", "DontCheck"):
            entry["fill_checking"] = str(fc)

        if path.startswith(prefix_attr) and path.count(".") == pref_dots + 2:
            if len(attrs) < MAX_ATTRS:
                attrs.append(entry)
            else:
                attrs_trunc = True
        elif path.startswith(prefix_dim) and path.count(".") == pref_dots + 2:
            if len(dimensions) < MAX_ATTRS:
                dimensions.append(entry)
        elif path.startswith(prefix_res) and path.count(".") == pref_dots + 2:
            if len(resources) < MAX_ATTRS:
                resources.append(entry)
        elif path.startswith(prefix_ts):
            rest = path[len(prefix_ts) :]
            parts = rest.split(".")
            if not parts or not parts[0]:
                continue
            ts_name = parts[0]
            ts = ts_map.get(ts_name)
            if ts is None:
                if len(ts_map) >= MAX_TABULAR:
                    ts_trunc = True
                    continue
                ts = {"name": ts_name, "columns": []}
                ts_map[ts_name] = ts
            if len(parts) == 1:
                if syn and syn != ts_name:
                    ts["synonym"] = syn
            elif len(parts) >= 3 and parts[1] in {"Реквизиты", "Attributes"}:
                col = {"name": parts[-1]}
                if syn and syn != parts[-1]:
                    col["synonym"] = syn
                if typ:
                    col["type"] = typ
                if fc and str(fc) not in ("", "DontCheck"):
                    col["fill_checking"] = str(fc)
                if len(ts["columns"]) < MAX_COLUMNS:
                    ts["columns"].append(col)

    movements: list[dict[str, Any]] = []
    for link in outgoing or []:
        if (link.get("link_type") or "") != "movements":
            continue
        ref = str(link.get("to_ref") or "")
        if not ref:
            continue
        movements.append({"path": ref})
        if len(movements) >= MAX_MOVEMENTS:
            break

    out: dict[str, Any] = {}
    if attrs:
        out["attrs"] = attrs
    if dimensions:
        out["dimensions"] = dimensions
    if resources:
        out["resources"] = resources
    tabular = list(ts_map.values())
    if tabular:
        out["tabular_sections"] = tabular
    if movements:
        out["movements"] = movements
    counts = {
        "attrs": len(attrs),
        "tabular_sections": len(tabular),
        "movements": len(movements),
    }
    if dimensions:
        counts["dimensions"] = len(dimensions)
    if resources:
        counts["resources"] = len(resources)
    out["counts"] = counts
    if attrs_trunc or ts_trunc:
        out["truncated"] = True
    return out


def links_by_type(outgoing: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    structure: dict[str, list[dict[str, Any]]] = {}
    for link in outgoing:
        structure.setdefault(link["link_type"], []).append(
            {"to_ref": link["to_ref"], "link_type": link["link_type"]}
        )
    return structure
