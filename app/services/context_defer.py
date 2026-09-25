"""Deferred MCP error-tier logging for recoverable context resolution failures."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.context_errors import DEFER_CODES
from app.services.context_key import normalize_context_key

DEFER_TTL_SEC = 90.0
COOLDOWN_SEC = 90.0


@dataclass
class _DeferSlot:
    input_context: str
    code: str
    allow_names: set[str]
    allow_tags: set[str]  # tag names without prefix
    created_at: float
    replay: str
    tool_name: str
    context_arg: str
    flushed: bool = False


_lock = threading.Lock()
_slots: list[_DeferSlot] = []
_cooldown_until: dict[tuple[str, str], float] = {}


def _slot_key(input_context: str, code: str) -> tuple[str, str]:
    return normalize_context_key(input_context), code


def _allow_from_candidates(
    candidates: list[dict[str, Any]], *, for_search: bool
) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    tags: set[str] = set()
    for c in candidates:
        n = (c.get("name") or "").strip()
        if n:
            names.add(n)
        for t in c.get("tags") or []:
            tt = (t or "").strip()
            if tt:
                tags.add(tt)
    if for_search:
        return names, tags
    return names, set()


def register_defer(
    *,
    input_context: str,
    code: str,
    candidates: list[dict[str, Any]],
    replay: str,
    tool_name: str,
    context_arg: str,
    for_search: bool,
    now: float | None = None,
) -> bool:
    """Return True if registered (eligible). False if cooldown blocks new slot."""
    if code not in DEFER_CODES or not candidates:
        return False
    ts = now if now is not None else time.monotonic()
    key = _slot_key(input_context, code)
    with _lock:
        cd = _cooldown_until.get(key, 0.0)
        if ts < cd:
            return False
        names, tags = _allow_from_candidates(candidates, for_search=for_search)
        _slots.append(
            _DeferSlot(
                input_context=input_context,
                code=code,
                allow_names=names,
                allow_tags=tags,
                created_at=ts,
                replay=replay,
                tool_name=tool_name,
                context_arg=context_arg,
            )
        )
    return True


def note_success_context(context: str, effective_context: str | None = None) -> None:
    canon = (effective_context or context or "").strip()
    if not canon:
        return
    tag_name: str | None = None
    if canon.lower().startswith("tag:"):
        tag_name = canon[4:].strip()
    with _lock:
        still: list[_DeferSlot] = []
        for slot in _slots:
            if slot.flushed:
                continue
            matched = canon in slot.allow_names or (
                tag_name is not None and tag_name in slot.allow_tags
            )
            if not matched:
                still.append(slot)
        _slots[:] = still


def flush_expired(now: float | None = None) -> list[_DeferSlot]:
    ts = now if now is not None else time.monotonic()
    flushed: list[_DeferSlot] = []
    with _lock:
        keep: list[_DeferSlot] = []
        for slot in _slots:
            if slot.flushed:
                continue
            if ts - slot.created_at >= DEFER_TTL_SEC:
                slot.flushed = True
                flushed.append(slot)
                key = _slot_key(slot.input_context, slot.code)
                _cooldown_until[key] = ts + COOLDOWN_SEC
            else:
                keep.append(slot)
        _slots[:] = keep
    return flushed


def on_repeat_input(input_context: str, code: str, replay: str, now: float | None = None) -> str | None:
    """Same input during cooldown: flush one error immediately, extend cooldown."""
    ts = now if now is not None else time.monotonic()
    key = _slot_key(input_context, code)
    with _lock:
        for slot in _slots:
            if slot.flushed:
                continue
            if _slot_key(slot.input_context, slot.code) == key:
                slot.flushed = True
                _cooldown_until[key] = ts + COOLDOWN_SEC
                return slot.replay
    return None
