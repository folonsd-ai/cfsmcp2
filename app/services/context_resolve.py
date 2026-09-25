"""MCP context / tag resolution (v2.2)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.database import connect
from app.core.config import settings
from app.repositories import entities as ent_repo
from app.repositories import tags as tag_repo
from app.services.context_errors import ContextResolveError
from app.services.context_key import normalize_context_key


@dataclass
class ContextResolution:
    effective_context: str
    tag: str | None
    entities: list[dict]
    matched_by: str
    input_context: str
    raw_context: str


def _candidate_row(entity: dict) -> dict[str, Any]:
    tags = entity.get("tags")
    if tags is None:
        tags = []
    return {"name": entity.get("name") or "", "tags": list(tags)}


def _candidates_from_names(conn: sqlite3.Connection, names: list[str]) -> list[dict]:
    out: list[dict] = []
    for n in names:
        row = ent_repo.get_entity_by_name(conn, n)
        if row:
            out.append({"name": row["name"], "tags": _entity_tags(conn, int(row["id"]))})
        else:
            out.append({"name": n, "tags": []})
    return out


def _entity_tags(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    ids = tag_repo.list_tag_ids_for_entity(conn, entity_id)
    names: list[str] = []
    for tid in ids:
        t = tag_repo.get_tag(conn, tid)
        if t:
            names.append(str(t["name"]))
    return names


def _ready_by_name_key(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = ent_repo.list_ready_contexts(conn)
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        key = normalize_context_key(str(r["name"]))
        by_key.setdefault(key, []).append(dict(r))
    return by_key


def _all_entities_by_key(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute("SELECT * FROM entities").fetchall()
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        key = normalize_context_key(str(d["name"]))
        by_key.setdefault(key, []).append(d)
    return by_key


def _tags_by_key(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    tags = tag_repo.list_tags(conn)
    by_key: dict[str, list[dict]] = {}
    for t in tags:
        key = normalize_context_key(str(t["name"]))
        by_key.setdefault(key, []).append(dict(t))
    return by_key


def _resolve_canonical_tag_name(conn: sqlite3.Connection, raw: str) -> str:
    exact = tag_repo.get_tag_by_name(conn, raw)
    if exact:
        return str(exact["name"])
    key = normalize_context_key(raw)
    by_key = _tags_by_key(conn)
    matches = by_key.get(key, [])
    if len(matches) > 1:
        raise ContextResolveError(
            "invariant_violation",
            "Multiple tags share the same normalized key (legacy data). Fix in UI.",
            input_context=raw,
            candidates=[],
        )
    if len(matches) == 1:
        return str(matches[0]["name"])
    raise ContextResolveError(
        "unknown",
        f"Unknown tag '{raw}'. Call list_context_groups.",
        input_context=raw,
        candidates=[],
        next_step="Use list_context_groups or a context name from list_contexts.",
    )


def resolve_explicit_tag(
    conn: sqlite3.Connection,
    raw_tag: str,
    *,
    for_get: bool,
    input_context: str,
) -> ContextResolution:
    canonical = _resolve_canonical_tag_name(conn, raw_tag)
    rows = ent_repo.list_ready_entities_for_tag(conn, canonical)
    effective = f"tag:{canonical}"
    if not rows:
        if not tag_repo.get_tag_by_name(conn, canonical):
            raise ContextResolveError(
                "unknown",
                f"Unknown tag '{raw_tag}'.",
                input_context=input_context,
                candidates=[],
            )
        raise ContextResolveError(
            "unknown",
            f"Tag '{canonical}' has no enabled ready contexts.",
            input_context=input_context,
            candidates=[],
        )
    if for_get:
        names = [str(r["name"]) for r in rows]
        cands = _candidates_from_names(conn, names)
        raise ContextResolveError(
            "tag_not_allowed_on_get",
            "tag:… is not allowed for get_* tools. Use a single context name.",
            input_context=input_context,
            candidates=cands,
            next_step="Copy name from candidates into context= for get_*.",
        )
    entities = [_validate_ready(dict(r), r["name"]) for r in rows]
    matched = "exact" if raw_tag == canonical else "normalized_name"
    return ContextResolution(
        effective_context=effective,
        tag=canonical,
        entities=entities,
        matched_by=matched if raw_tag == canonical else "normalized_name",
        input_context=input_context,
        raw_context=input_context,
    )


def _validate_ready(entity: dict, context: str) -> dict:
    if not entity["enabled"]:
        raise ValueError(f"Context '{context}' is disabled. Enable it in the UI.")
    if entity["status"] != "ready":
        raise ValueError(
            f"Context '{context}' status is '{entity['status']}', need 'ready'. Run reindex."
        )
    return dict(entity)


def resolve_name_hint(
    conn: sqlite3.Connection,
    raw: str,
    *,
    for_get: bool,
    input_context: str,
) -> ContextResolution:
    from app.services.search import _suggest_context_candidates

    exact_row = ent_repo.get_entity_by_name(conn, raw)
    if exact_row:
        ent = _validate_ready(dict(exact_row), raw)
        return ContextResolution(
            effective_context=str(ent["name"]),
            tag=None,
            entities=[ent],
            matched_by="exact",
            input_context=input_context,
            raw_context=input_context,
        )

    key = normalize_context_key(raw)
    all_by_key = _all_entities_by_key(conn)
    if len(all_by_key.get(key, [])) > 1:
        raise ContextResolveError(
            "invariant_violation",
            "Multiple contexts share the same normalized key (legacy data). Fix in UI.",
            input_context=input_context,
            candidates=[],
        )

    if key in all_by_key and key in _tags_by_key(conn):
        raise ContextResolveError(
            "invariant_violation",
            "Context and tag share the same normalized key (legacy data). Fix in UI.",
            input_context=input_context,
            candidates=[],
        )

    ready_by_key = _ready_by_name_key(conn)
    ready_matches = ready_by_key.get(key, [])
    if len(ready_matches) == 1:
        row = ready_matches[0]
        full = ent_repo.get_entity(conn, int(row["id"])) or row
        ent = _validate_ready(dict(full), row["name"])
        matched = "exact" if raw == ent["name"] else "normalized_name"
        return ContextResolution(
            effective_context=str(ent["name"]),
            tag=None,
            entities=[ent],
            matched_by=matched,
            input_context=input_context,
            raw_context=input_context,
        )
    if len(ready_matches) > 1:
        raise ContextResolveError(
            "invariant_violation",
            "Multiple ready contexts share the same normalized key (legacy data).",
            input_context=input_context,
            candidates=[],
        )

    tags_key = _tags_by_key(conn)
    tag_matches = tags_key.get(key, [])
    if len(tag_matches) > 1:
        raise ContextResolveError(
            "invariant_violation",
            "Multiple tags share the same normalized key (legacy data).",
            input_context=input_context,
            candidates=[],
        )
    if len(tag_matches) == 1:
        canonical_tag = str(tag_matches[0]["name"])
        rows = ent_repo.list_ready_entities_for_tag(conn, canonical_tag)
        if for_get:
            if len(rows) == 1:
                ent = _validate_ready(dict(rows[0]), rows[0]["name"])
                matched = "normalized_name" if raw != ent["name"] else "exact"
                return ContextResolution(
                    effective_context=str(ent["name"]),
                    tag=None,
                    entities=[ent],
                    matched_by=matched,
                    input_context=input_context,
                    raw_context=input_context,
                )
            names = [str(r["name"]) for r in rows]
            raise ContextResolveError(
                "ambiguous",
                f"Tag '{canonical_tag}' maps to several contexts. Pick one name.",
                input_context=input_context,
                candidates=_candidates_from_names(conn, names),
                next_step="Copy one name from candidates into context=.",
            )
        effective = f"tag:{canonical_tag}"
        if not rows:
            raise ContextResolveError(
                "unknown",
                f"Tag '{canonical_tag}' has no enabled ready contexts.",
                input_context=input_context,
                candidates=[],
            )
        entities = [_validate_ready(dict(r), r["name"]) for r in rows]
        return ContextResolution(
            effective_context=effective,
            tag=canonical_tag,
            entities=entities,
            matched_by="by_tag",
            input_context=input_context,
            raw_context=input_context,
        )

    suggest = _suggest_context_candidates(raw, conn)
    if suggest:
        raise ContextResolveError(
            "ambiguous",
            f"Unknown context '{raw}'. Pick exact name from candidates.",
            input_context=input_context,
            candidates=_candidates_from_names(conn, suggest),
            next_step="Copy name from candidates; do not retype.",
        )

    raise ContextResolveError(
        "unknown",
        f"Unknown context '{raw}'. Call list_contexts or list_context_groups.",
        input_context=input_context,
        candidates=[],
        next_step="Use list_contexts when the context is unknown.",
    )


def resolve_context_group_raw(
    context: str,
    conn: sqlite3.Connection | None = None,
    *,
    for_get: bool = False,
) -> ContextResolution:
    from app.services.search import parse_context_ref

    raw = (context or "").strip()
    if not raw:
        raise ContextResolveError(
            "unknown",
            "Context must not be empty",
            input_context=raw,
            candidates=[],
        )
    kind, value = parse_context_ref(raw)
    own = conn is None
    if own:
        conn = connect(settings.db_path)
    assert conn is not None
    try:
        if kind == "tag":
            return resolve_explicit_tag(
                conn, value, for_get=for_get, input_context=raw
            )
        return resolve_name_hint(
            conn, value, for_get=for_get, input_context=raw
        )
    finally:
        if own:
            conn.close()
