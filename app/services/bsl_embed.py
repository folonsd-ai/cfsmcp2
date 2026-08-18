"""How BSL method bodies participate in embedding text."""

from __future__ import annotations

import json
from typing import Any

BSL_EMBED_META = "meta"
BSL_EMBED_BODY = "body"
BSL_EMBED_CHUNKS = "chunks"

ALLOWED_BSL_EMBED_MODES = frozenset({BSL_EMBED_META, BSL_EMBED_BODY, BSL_EMBED_CHUNKS})
DEFAULT_BSL_EMBED_MODE = BSL_EMBED_META

# Defaults: slightly aggressive char budget for e5 (~512 tokens) without a tokenizer.
DEFAULT_PASSAGE_MAX_CHARS = 1350
DEFAULT_CHUNK_SIZE = 1100
DEFAULT_CHUNK_OVERLAP = 130
DEFAULT_MIN_BODY_CHARS = 200
DEFAULT_MAX_CHUNKS = 12

# Absolute clamps (UI + API). Deletes always cover up to MAX_CHUNKS_HARD.
PASSAGE_MAX_CHARS_MIN, PASSAGE_MAX_CHARS_MAX = 400, 4000
CHUNK_SIZE_MIN, CHUNK_SIZE_MAX = 200, 3500
CHUNK_OVERLAP_MIN, CHUNK_OVERLAP_MAX = 0, 2000
MIN_BODY_CHARS_MIN, MIN_BODY_CHARS_MAX = 50, 800
MAX_CHUNKS_MIN, MAX_CHUNKS_HARD = 1, 32

# Recommended char budgets for common embedding context windows (no tokenizer:
# ~2.7–2.9 chars/token for RU/BSL, ~8–10% headroom for meta/special tokens).
BSL_EMBED_WINDOW_PRESETS: list[dict[str, Any]] = [
    {
        "id": "512",
        "tokens": 512,
        "models_hint": "e5-small / e5-base / multilingual-e5-small",
        "limits": {
            "passage_max_chars": 1350,
            "chunk_size": 1100,
            "chunk_overlap": 130,
            "min_body_chars": 200,
            "max_chunks": 12,
        },
    },
    {
        "id": "768",
        "tokens": 768,
        "models_hint": "mid-size embedding models",
        "limits": {
            "passage_max_chars": 2000,
            "chunk_size": 1650,
            "chunk_overlap": 180,
            "min_body_chars": 250,
            "max_chunks": 12,
        },
    },
    {
        "id": "1024",
        "tokens": 1024,
        "models_hint": "e5-large (if 1024 ctx) / bge short mode",
        "limits": {
            "passage_max_chars": 2700,
            "chunk_size": 2200,
            "chunk_overlap": 250,
            "min_body_chars": 300,
            "max_chunks": 14,
        },
    },
    {
        "id": "2048",
        "tokens": 2048,
        "models_hint": "long-context embedders (nomic / bge-m3, …)",
        "limits": {
            "passage_max_chars": 4000,
            "chunk_size": 3300,
            "chunk_overlap": 350,
            "min_body_chars": 400,
            "max_chunks": 16,
        },
    },
]

# Back-compat aliases for imports / docs
PASSAGE_MAX_CHARS = DEFAULT_PASSAGE_MAX_CHARS
CHUNK_SIZE = DEFAULT_CHUNK_SIZE
CHUNK_OVERLAP = DEFAULT_CHUNK_OVERLAP
MIN_BODY_CHARS = DEFAULT_MIN_BODY_CHARS
MAX_CHUNKS = DEFAULT_MAX_CHUNKS


def normalize_bsl_embed_mode(raw: str | None) -> str:
    m = (raw or "").strip().lower()
    return m if m in ALLOWED_BSL_EMBED_MODES else DEFAULT_BSL_EMBED_MODE


def default_bsl_embed_limits() -> dict[str, int]:
    return {
        "passage_max_chars": DEFAULT_PASSAGE_MAX_CHARS,
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        "min_body_chars": DEFAULT_MIN_BODY_CHARS,
        "max_chunks": DEFAULT_MAX_CHUNKS,
    }


def bsl_embed_limits_bounds() -> dict[str, dict[str, int]]:
    return {
        "passage_max_chars": {
            "min": PASSAGE_MAX_CHARS_MIN,
            "max": PASSAGE_MAX_CHARS_MAX,
            "default": DEFAULT_PASSAGE_MAX_CHARS,
        },
        "chunk_size": {
            "min": CHUNK_SIZE_MIN,
            "max": CHUNK_SIZE_MAX,
            "default": DEFAULT_CHUNK_SIZE,
        },
        "chunk_overlap": {
            "min": CHUNK_OVERLAP_MIN,
            "max": CHUNK_OVERLAP_MAX,
            "default": DEFAULT_CHUNK_OVERLAP,
        },
        "min_body_chars": {
            "min": MIN_BODY_CHARS_MIN,
            "max": MIN_BODY_CHARS_MAX,
            "default": DEFAULT_MIN_BODY_CHARS,
        },
        "max_chunks": {
            "min": MAX_CHUNKS_MIN,
            "max": MAX_CHUNKS_HARD,
            "default": DEFAULT_MAX_CHUNKS,
        },
    }


def bsl_embed_window_presets() -> list[dict[str, Any]]:
    """Presets for UI: each limits dict is already normalized/clamped."""
    out: list[dict[str, Any]] = []
    for preset in BSL_EMBED_WINDOW_PRESETS:
        lim = normalize_bsl_embed_limits(base=dict(preset["limits"]))
        out.append(
            {
                "id": preset["id"],
                "tokens": int(preset["tokens"]),
                "models_hint": preset.get("models_hint") or "",
                "limits": lim,
            }
        )
    return out


def normalize_bsl_embed_limits(
    *,
    passage_max_chars: int | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    min_body_chars: int | None = None,
    max_chunks: int | None = None,
    base: dict[str, int] | None = None,
) -> dict[str, int]:
    """Clamp and reconcile limits so chunk/meta budgets stay coherent."""
    cur = dict(base or default_bsl_embed_limits())
    if passage_max_chars is not None:
        cur["passage_max_chars"] = int(passage_max_chars)
    if chunk_size is not None:
        cur["chunk_size"] = int(chunk_size)
    if chunk_overlap is not None:
        cur["chunk_overlap"] = int(chunk_overlap)
    if min_body_chars is not None:
        cur["min_body_chars"] = int(min_body_chars)
    if max_chunks is not None:
        cur["max_chunks"] = int(max_chunks)

    passage = max(PASSAGE_MAX_CHARS_MIN, min(PASSAGE_MAX_CHARS_MAX, int(cur["passage_max_chars"])))
    min_body = max(MIN_BODY_CHARS_MIN, min(MIN_BODY_CHARS_MAX, int(cur["min_body_chars"])))
    # Leave room for meta + newline + min body
    min_body = min(min_body, max(MIN_BODY_CHARS_MIN, passage - 2))
    chunk = max(CHUNK_SIZE_MIN, min(CHUNK_SIZE_MAX, int(cur["chunk_size"])))
    chunk = min(chunk, max(CHUNK_SIZE_MIN, passage - 1))
    overlap = max(CHUNK_OVERLAP_MIN, min(CHUNK_OVERLAP_MAX, int(cur["chunk_overlap"])))
    overlap = min(overlap, max(0, chunk - 1))
    nchunks = max(MAX_CHUNKS_MIN, min(MAX_CHUNKS_HARD, int(cur["max_chunks"])))
    return {
        "passage_max_chars": passage,
        "chunk_size": chunk,
        "chunk_overlap": overlap,
        "min_body_chars": min_body,
        "max_chunks": nchunks,
    }


def get_bsl_embed_limits() -> dict[str, int]:
    """Effective limits from app_settings (or defaults)."""
    try:
        from app.services import runtime_settings

        return runtime_settings.get_bsl_embed_limits()
    except Exception:
        return default_bsl_embed_limits()


def _props(obj: dict[str, Any]) -> dict[str, Any]:
    raw = obj.get("props_json") or obj.get("props") or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _is_method(obj: dict[str, Any]) -> bool:
    return (obj.get("kind") or "") in {"Procedure", "Function"}


_SIGNATURE_TEXT_MAX = 800


def meta_text(obj: dict[str, Any]) -> str:
    parts = [
        obj.get("path") or "",
        obj.get("name") or "",
        obj.get("synonym") or "",
        obj.get("comment") or "",
    ]
    if _is_method(obj):
        # Parameter names live in props.signature, not synonym/comment.
        # Without this, typical methods embed as bare path|name (2026-08-17).
        sig = str(_props(obj).get("signature") or "").strip()
        if sig:
            if len(sig) > _SIGNATURE_TEXT_MAX:
                sig = sig[:_SIGNATURE_TEXT_MAX].rstrip()
            parts.append(sig)
    base = " | ".join(p for p in parts if p)
    if (obj.get("kind") or "") == "ManagedForm":
        form = form_structure_text(obj)
        if form:
            return f"{base}\n{form}" if base else form
    return base


_FORM_TEXT_MAX = 1800


def form_structure_text(obj: dict[str, Any]) -> str:
    """Gap 3: searchable text from ManagedForm ``props_json`` summary.

    Built by ``dump_assets_parser._summarize_form`` (elements/commands/attrs/events).
    Cap length so embedding passages stay bounded.
    """
    props = _props(obj)
    chunks: list[str] = []
    for key in ("attributes", "commands"):
        vals = props.get(key) or []
        if isinstance(vals, list) and vals:
            names = [str(v) for v in vals if v][:80]
            if names:
                chunks.append(f"{key}: " + " ".join(names))
    elems = props.get("elements") or []
    if isinstance(elems, list) and elems:
        names = []
        for el in elems[:100]:
            if isinstance(el, dict):
                nm = el.get("name") or ""
                tp = el.get("type") or ""
                if nm:
                    names.append(f"{nm}" + (f"({tp})" if tp else ""))
            elif el:
                names.append(str(el))
        if names:
            chunks.append("elements: " + " ".join(names))
    events = props.get("events") or []
    if isinstance(events, list) and events:
        ev_parts = []
        for ev in events[:40]:
            if isinstance(ev, dict):
                nm = ev.get("name") or ""
                handler = ev.get("handler") or ""
                if nm or handler:
                    ev_parts.append(f"{nm}:{handler}" if handler else nm)
        if ev_parts:
            chunks.append("events: " + " ".join(ev_parts))
    types = props.get("element_types") or {}
    if isinstance(types, dict) and types:
        chunks.append(
            "types: "
            + " ".join(f"{k}:{v}" for k, v in list(types.items())[:30])
        )
    text = " | ".join(chunks)
    if len(text) > _FORM_TEXT_MAX:
        return text[:_FORM_TEXT_MAX].rstrip()
    return text


def _method_body(obj: dict[str, Any]) -> str:
    return str(_props(obj).get("body") or "").strip()


def clip_meta(
    meta: str,
    *,
    passage_max: int,
    min_body: int,
) -> str:
    """Keep meta short enough that at least min_body of body can fit."""
    meta = meta or ""
    max_meta = max(0, passage_max - 1 - max(0, min_body))
    if len(meta) <= max_meta:
        return meta
    return meta[:max_meta].rstrip()


def body_budget(meta: str, *, passage_max: int) -> int:
    """Chars left for method body after meta (and the joining newline)."""
    meta = meta or ""
    used = len(meta) + (1 if meta else 0)
    return max(0, passage_max - used)


def split_chunks(
    text: str,
    *,
    size: int,
    overlap: int,
    max_chunks: int,
) -> list[str]:
    """Split text into overlapping windows (character-based)."""
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0:
        return [text]
    overlap = max(0, min(overlap, size - 1))
    step = max(1, size - overlap)
    out: list[str] = []
    i = 0
    n = len(text)
    limit = max(1, int(max_chunks))
    while i < n and len(out) < limit:
        piece = text[i : i + size].strip()
        if piece:
            out.append(piece)
        if i + size >= n:
            break
        i += step
    return out


def zvec_doc_ids_for_object(obj_id: int | str) -> list[str]:
    """All zvec doc ids that may exist for an object (base + chunk suffixes).

    Uses the hard max so lowering max_chunks in settings still cleans old docs
    when the *object* is deleted. Hot reindex path uses ``zvec_stale_chunk_ids``.
    """
    oid = str(obj_id)
    return [oid] + [f"{oid}#{i}" for i in range(1, MAX_CHUNKS_HARD)]


def zvec_stale_chunk_ids(
    obj_id: int | str,
    kept_ids: list[str],
    *,
    max_chunks: int | None = None,
) -> list[str]:
    """Chunk ids to delete after a shorter split (not the ids we are upserting).

    Caps at current ``max_chunks`` (not HARD 32) so incremental re-embed does
    not issue tens of no-op deletes per object.
    """
    oid = str(obj_id)
    kept = {str(x) for x in (kept_ids or []) if x}
    limit = int(max_chunks) if max_chunks is not None else MAX_CHUNKS
    limit = max(1, min(int(limit), MAX_CHUNKS_HARD))
    candidates = [oid] + [f"{oid}#{i}" for i in range(1, limit)]
    return [c for c in candidates if c not in kept]


def _help_meta(obj: dict[str, Any]) -> str:
    """Мета help-чанка: path | name | synonym (без comment — он идёт отдельно)."""
    parts = [
        obj.get("path") or "",
        obj.get("name") or "",
        obj.get("synonym") or "",
    ]
    return " | ".join(p for p in parts if p)


def passages_for_object(
    obj: dict[str, Any],
    mode: str,
    *,
    limits: dict[str, int] | None = None,
) -> list[tuple[str, str]]:
    """Return (zvec_doc_id, embedding_text) pairs for one object."""
    mode = normalize_bsl_embed_mode(mode)
    lim = normalize_bsl_embed_limits(base=limits or get_bsl_embed_limits())
    passage_max = lim["passage_max_chars"]
    chunk_size = lim["chunk_size"]
    chunk_overlap = lim["chunk_overlap"]
    min_body = lim["min_body_chars"]
    max_chunks = lim["max_chunks"]

    oid = str(obj.get("id") or "")
    base = meta_text(obj)
    if not oid:
        return [(oid, base)] if base else []

    # U10: справка — это чанк с текстом в comment (до 2600 символов). Разбиваем
    # длинные разделы на перекрывающиеся окна, как у BSL body.
    if (obj.get("kind") or "") == "Help":
        body = str(obj.get("comment") or "").strip()
        if not body:
            return [(oid, base)]
        meta = _help_meta(obj)
        budget = max(0, passage_max - len(meta) - 1) if meta else passage_max
        if budget <= 0:
            return [(oid, meta)] if meta else []
        if len(body) <= budget:
            return [(oid, f"{meta}\n{body}" if meta else body)]
        win = min(chunk_size, budget)
        pieces = split_chunks(
            body,
            size=win,
            overlap=min(chunk_overlap, max(0, win - 1)),
            max_chunks=max_chunks,
        )
        out: list[tuple[str, str]] = []
        for i, piece in enumerate(pieces):
            doc_id = oid if i == 0 else f"{oid}#{i}"
            out.append((doc_id, f"{meta}\n{piece}" if meta else piece))
        return out

    if mode == BSL_EMBED_META or not _is_method(obj):
        return [(oid, base)]

    body = _method_body(obj)
    if not body:
        return [(oid, base)]

    base = clip_meta(base, passage_max=passage_max, min_body=min_body)
    budget = body_budget(base, passage_max=passage_max)
    if budget <= 0:
        return [(oid, base)] if base else []

    if mode == BSL_EMBED_BODY:
        clipped = body if len(body) <= budget else body[:budget]
        text = f"{base}\n{clipped}" if base else clipped
        return [(oid, text)]

    win = min(chunk_size, budget)
    pieces = split_chunks(
        body,
        size=win,
        overlap=min(chunk_overlap, max(0, win - 1)),
        max_chunks=max_chunks,
    )
    if not pieces:
        return [(oid, base)]
    out: list[tuple[str, str]] = []
    for i, piece in enumerate(pieces):
        doc_id = oid if i == 0 else f"{oid}#{i}"
        text = f"{base}\n{piece}" if base else piece
        out.append((doc_id, text))
    return out
