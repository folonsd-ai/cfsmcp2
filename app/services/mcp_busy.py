"""In-flight MCP tool registry + lock-wait accounting (observability only).

Heartbeat variant (c): no per-tick app_log rows — running_for_ms is derived
from started_at on snapshot; completion still uses maybe_slow as before.
"""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections import deque
from typing import Any

_lock = threading.Lock()
# call_id -> entry dict
_inflight: dict[str, dict[str, Any]] = {}
# Recent completions for "many short tools" window (in-memory only).
_recent: deque[dict[str, Any]] = deque(maxlen=64)
_current_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_busy_call_id", default=None
)

# Entity statuses that mean parse/index/embed is running (existing DB field).
_BUSY_ENTITY_STATUSES = frozenset({"parsing", "indexing", "loading_modules"})


def begin(tool: str, *, context: str = "") -> str:
    """Register an in-flight MCP tool call. Returns call_id."""
    call_id = uuid.uuid4().hex
    now = time.time()
    entry = {
        "call_id": call_id,
        "tool": (tool or "")[:200],
        "context": (context or "")[:200],
        "started_at": now,
        "started_mono": time.perf_counter(),
        "sqlite_wait_ms": 0.0,
        "zvec_wait_ms": 0.0,
        "sqlite_lock_retries": 0,
        "waiting_on_lock": None,  # "sqlite" | "zvec" | None
    }
    with _lock:
        _inflight[call_id] = entry
    _current_call_id.set(call_id)
    return call_id


def end(call_id: str | None) -> dict[str, Any] | None:
    """Unregister; return a summary suitable for replay detail / recent buffer."""
    if not call_id:
        return None
    with _lock:
        entry = _inflight.pop(call_id, None)
    try:
        token_cur = _current_call_id.get()
        if token_cur == call_id:
            _current_call_id.set(None)
    except Exception:
        pass
    if entry is None:
        return None
    running_ms = (time.perf_counter() - float(entry["started_mono"])) * 1000.0
    summary = {
        "call_id": call_id,
        "tool": entry.get("tool") or "",
        "context": entry.get("context") or "",
        "duration_ms": round(running_ms, 1),
        "sqlite_wait_ms": round(float(entry.get("sqlite_wait_ms") or 0), 1),
        "zvec_wait_ms": round(float(entry.get("zvec_wait_ms") or 0), 1),
        "sqlite_lock_retries": int(entry.get("sqlite_lock_retries") or 0),
        "ended_at": time.time(),
    }
    with _lock:
        _recent.appendleft(summary)
    return summary


def add_sqlite_wait(ms: float, *, retries: int = 1) -> None:
    """Accumulate SQLite lock-retry sleep (lower bound; busy_timeout not included)."""
    if ms <= 0:
        return
    call_id = _current_call_id.get()
    if not call_id:
        return
    with _lock:
        entry = _inflight.get(call_id)
        if not entry:
            return
        entry["sqlite_wait_ms"] = float(entry.get("sqlite_wait_ms") or 0) + float(ms)
        entry["sqlite_lock_retries"] = int(entry.get("sqlite_lock_retries") or 0) + int(
            retries
        )
        entry["waiting_on_lock"] = "sqlite"


def add_zvec_wait(ms: float) -> None:
    if ms <= 0:
        return
    call_id = _current_call_id.get()
    if not call_id:
        return
    with _lock:
        entry = _inflight.get(call_id)
        if not entry:
            return
        entry["zvec_wait_ms"] = float(entry.get("zvec_wait_ms") or 0) + float(ms)
        entry["waiting_on_lock"] = "zvec"


def clear_waiting_flag() -> None:
    call_id = _current_call_id.get()
    if not call_id:
        return
    with _lock:
        entry = _inflight.get(call_id)
        if entry:
            entry["waiting_on_lock"] = None


def _entry_view(entry: dict[str, Any], *, now_mono: float) -> dict[str, Any]:
    running_ms = (now_mono - float(entry["started_mono"])) * 1000.0
    return {
        "call_id": entry["call_id"],
        "tool": entry.get("tool") or "",
        "context": entry.get("context") or "",
        "started_at": entry.get("started_at"),
        "running_for_ms": round(running_ms, 1),
        "sqlite_wait_ms": round(float(entry.get("sqlite_wait_ms") or 0), 1),
        "zvec_wait_ms": round(float(entry.get("zvec_wait_ms") or 0), 1),
        "sqlite_lock_retries": int(entry.get("sqlite_lock_retries") or 0),
        "waiting_on_lock": entry.get("waiting_on_lock"),
    }


def _index_busy_from_db() -> dict[str, Any]:
    """Reuse entities.status — do not invent a second busy flag."""
    try:
        from app.core.config import settings
        from app.core.database import connect

        conn = connect(settings.db_path)
        try:
            rows = conn.execute(
                """
                SELECT id, name, status FROM entities
                WHERE status IN ('parsing', 'indexing', 'loading_modules')
                ORDER BY id
                LIMIT 20
                """
            ).fetchall()
        finally:
            conn.close()
        items = [
            {
                "id": int(r["id"]),
                "name": str(r["name"] or ""),
                "status": str(r["status"] or ""),
            }
            for r in rows
        ]
        return {
            "embed_reindex_busy": bool(items),
            "busy_entities": items,
        }
    except Exception as exc:
        return {
            "embed_reindex_busy": False,
            "busy_entities": [],
            "busy_entities_error": str(exc)[:200],
        }


def snapshot(*, recent_window_sec: float = 60.0) -> dict[str, Any]:
    """Concurrent cut: in-flight tools + index busy + recent completions."""
    now = time.time()
    now_mono = time.perf_counter()
    with _lock:
        inflight = [_entry_view(e, now_mono=now_mono) for e in _inflight.values()]
        recent_all = list(_recent)
    inflight.sort(key=lambda x: float(x.get("running_for_ms") or 0), reverse=True)
    cutoff = now - float(recent_window_sec)
    recent = [r for r in recent_all if float(r.get("ended_at") or 0) >= cutoff]
    short = [r for r in recent if float(r.get("duration_ms") or 0) < 500.0]
    index_busy = _index_busy_from_db()
    return {
        "ts": now,
        "heartbeat": "in_memory_only",
        "mcp_in_flight_count": len(inflight),
        "in_flight": inflight,
        "embed_reindex_busy": index_busy.get("embed_reindex_busy", False),
        "busy_entities": index_busy.get("busy_entities", []),
        "recent_window_sec": float(recent_window_sec),
        "recent_completed_count": len(recent),
        "recent_short_count": len(short),
        "recent_completed_ms_sum": round(
            sum(float(r.get("duration_ms") or 0) for r in recent), 1
        ),
        "recent_completed": recent[:20],
    }


def reset_for_tests() -> None:
    """Clear registry (unit tests only)."""
    with _lock:
        _inflight.clear()
        _recent.clear()
    _current_call_id.set(None)
