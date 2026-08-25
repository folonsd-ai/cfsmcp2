# -*- coding: utf-8 -*-
"""Persisted app log: errors + slow requests (survives restarts)."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.core.config import settings
from app.core.database import connect

log = logging.getLogger("cfsmcp2.app_log")

# Enough for tool args JSON + result summary so a slow call can be replayed offline.
MAX_DETAIL_CHARS = 12_000
MAX_ARGS_CHARS = 8_000
MAX_RESULT_SUMMARY_CHARS = 1_200
DEFAULT_SLOW_MS = 3000
DEFAULT_RETAIN_DAYS = 14
DEFAULT_MAX_ROWS = 5000
MIN_SLOW_MS = 500
MAX_SLOW_MS = 60_000
PERSIST_TIERS = frozenset({"error", "slow", "info"})
MIN_RETAIN_DAYS = 1
MAX_RETAIN_DAYS = 90
MIN_MAX_ROWS = 100
MAX_MAX_ROWS = 50_000

_lock = threading.Lock()
_prune_counter = 0


def _clip(detail: str) -> str:
    """Trim length but keep newlines (needed for replayable JSON args)."""
    s = str(detail or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in s.split("\n")]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if blank:
                continue
            blank = True
            out.append("")
        else:
            blank = False
            out.append(ln)
    s = "\n".join(out).strip("\n")
    if len(s) > MAX_DETAIL_CHARS:
        return s[: MAX_DETAIL_CHARS - 1] + "…"
    return s


def _json_dump(value: Any, *, limit: int) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    except Exception as exc:
        raw = f"<json-error {type(value).__name__}: {exc}>"
    if len(raw) > limit:
        return raw[: limit - 1] + "…"
    return raw


def summarize_result(value: Any, *, limit: int = MAX_RESULT_SUMMARY_CHARS) -> str:
    """Compact result shape for logs — not a full dump (can be huge)."""
    try:
        if value is None:
            return "null"
        if isinstance(value, (str, int, float, bool)):
            s = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, dict):
            keys = list(value.keys())
            parts: list[str] = [f"keys={keys[:20]}"]
            if value.get("error") is not None:
                parts.append(f"error={value.get('error')!s}")
            if value.get("bottleneck"):
                parts.append(f"bottleneck={value.get('bottleneck')}")
            timing = value.get("timing_ms")
            if isinstance(timing, dict) and timing.get("total") is not None:
                parts.append(f"timing_total={timing.get('total')}")
            for list_key in ("items", "results", "hits", "methods", "modules", "objects", "usages"):
                arr = value.get(list_key)
                if isinstance(arr, list):
                    parts.append(f"{list_key}={len(arr)}")
            for n_key in ("total", "count", "limit", "offset", "truncated", "match_mode"):
                if n_key in value:
                    parts.append(f"{n_key}={value.get(n_key)!s}")
            try:
                # Prefer compact diagnostic summary over dumping huge results.
                slim = {
                    k: value[k]
                    for k in (
                        "bottleneck",
                        "hint",
                        "timing_ms",
                        "scale",
                        "match_mode",
                        "total",
                        "truncated",
                        "sql_recall_budget_hit",
                        "stem_recall_budget_hit",
                        "sql_recall_skipped",
                        "next_tool",
                    )
                    if k in value
                }
                full = json.dumps(slim or value, ensure_ascii=False, default=str)
            except Exception:
                full = ""
            if full and len(full) <= limit and slim:
                s = full
            elif full and len(full) <= limit:
                s = full
            else:
                s = "; ".join(parts)
        elif isinstance(value, list):
            s = f"list[{len(value)}]"
            if value:
                first = _json_dump(value[0], limit=min(400, limit // 2))
                s += f" first={first}"
        else:
            s = repr(value)
    except Exception as exc:
        s = f"<unrepr {type(value).__name__}: {exc}>"
    s = str(s)
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def format_mcp_replay_detail(
    *,
    tool: str,
    context: str = "",
    duration_ms: float,
    threshold_ms: float | None = None,
    args: Any = None,
    result: Any = None,
    error: str = "",
    ok: bool = True,
    busy: dict | None = None,
) -> str:
    """Multi-line detail: enough to replay the MCP tool call without the DB UI."""
    if threshold_ms is None:
        try:
            from app.services import runtime_settings

            threshold_ms = float(runtime_settings.get_slow_request_ms())
        except Exception:
            threshold_ms = float(DEFAULT_SLOW_MS)

    lines: list[str] = [
        f"ms={duration_ms:.0f} threshold_ms={threshold_ms:.0f} ok={'yes' if ok else 'no'}",
        f"tool={tool}",
        f"context={context or ''}",
    ]
    if error:
        lines.append(f"error={error}")

    if isinstance(busy, dict) and busy:
        sw = float(busy.get("sqlite_wait_ms") or 0)
        zw = float(busy.get("zvec_wait_ms") or 0)
        if sw > 0 or zw > 0 or int(busy.get("sqlite_lock_retries") or 0):
            lines.append(
                "busy_wait="
                f"sqlite_ms={sw:.0f} zvec_ms={zw:.0f} "
                f"sqlite_retries={int(busy.get('sqlite_lock_retries') or 0)}"
            )

    # Prefer measured phase timing from the tool result (search / usages / …).
    if isinstance(result, dict):
        bottleneck = result.get("bottleneck")
        timing = result.get("timing_ms")
        scale = result.get("scale")
        hint = result.get("hint")
        if bottleneck:
            lines.append(f"bottleneck={bottleneck}")
        if hint:
            lines.append(f"hint={hint}")
        if result.get("sql_recall_budget_hit"):
            lines.append("sql_recall_budget_hit=yes")
        if result.get("stem_recall_budget_hit"):
            lines.append("stem_recall_budget_hit=yes")
        if result.get("sql_recall_skipped"):
            lines.append(f"sql_recall_skipped={result.get('sql_recall_skipped')}")
        if isinstance(timing, dict) and timing:
            lines.append("timing_ms=")
            lines.append(_json_dump(timing, limit=800))
        if isinstance(scale, dict) and scale:
            lines.append("scale=")
            lines.append(_json_dump(scale, limit=800))

    lines.append("args=")
    lines.append(_json_dump(args if args is not None else {}, limit=MAX_ARGS_CHARS))
    lines.append("result_summary=")
    lines.append(summarize_result(result))
    lines.append("reproduce=")
    lines.append(
        "Call MCP tool with the args JSON above (same tool + context). "
        "bottleneck/timing_ms explain where time went; no DB dump needed for that."
    )
    return "\n".join(lines)


def format_index_job_detail(
    *,
    job: str,
    duration_ms: float,
    timing_ms: dict | None = None,
    bottleneck: str = "",
    scale: dict | None = None,
    note: str = "",
) -> str:
    """Completed parse/reindex: phase timing without MCP args."""
    lines = [
        f"ms={float(duration_ms):.0f} ok=yes",
        f"job={job}",
    ]
    if bottleneck:
        lines.append(f"bottleneck={bottleneck}")
    if isinstance(timing_ms, dict) and timing_ms:
        lines.append("timing_ms=")
        lines.append(_json_dump(timing_ms, limit=800))
    if isinstance(scale, dict) and scale:
        lines.append("scale=")
        lines.append(_json_dump(scale, limit=800))
    if note:
        lines.append(f"note={note}")
    return "\n".join(lines)


def format_api_replay_detail(
    *,
    method: str,
    path: str,
    duration_ms: float,
    threshold_ms: float | None = None,
    query: str = "",
    status: int = 0,
    detail: str = "",
    ok: bool = True,
) -> str:
    if threshold_ms is None:
        try:
            from app.services import runtime_settings

            threshold_ms = float(runtime_settings.get_slow_request_ms())
        except Exception:
            threshold_ms = float(DEFAULT_SLOW_MS)
    lines = [
        f"ms={duration_ms:.0f} threshold_ms={threshold_ms:.0f} ok={'yes' if ok else 'no'}",
        f"method={method}",
        f"path={path}",
    ]
    if status:
        lines.append(f"status={status}")
    if query:
        lines.append(f"query={query}")
    if detail:
        lines.append(f"note={detail}")
    lines.append("reproduce=")
    q = f"?{query}" if query else ""
    lines.append(f"{method} {path}{q}")
    lines.append(
        "Request body is not captured in v1 — use path/query; for MCP prefer tool logs."
    )
    return "\n".join(lines)


def append(
    *,
    tier: str,
    kind: str,
    name: str,
    ok: bool = True,
    duration_ms: float = 0.0,
    context: str = "",
    detail: str = "",
) -> None:
    """Best-effort insert; never raises to callers."""
    tier = (tier or "").strip().lower()
    if tier not in PERSIST_TIERS:
        return
    try:
        from app.services import runtime_settings

        if not runtime_settings.get_app_log_enabled():
            return
    except Exception:
        pass

    now = time.time()
    try:
        conn = connect(settings.db_path)
        try:
            conn.execute(
                """
                INSERT INTO app_log(ts, tier, kind, name, ok, duration_ms, context, detail)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    tier,
                    (kind or "")[:64],
                    (name or "")[:200],
                    1 if ok else 0,
                    float(duration_ms or 0.0),
                    (context or "")[:200],
                    _clip(detail),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("app_log append failed: %s", exc)
        return

    global _prune_counter
    with _lock:
        _prune_counter += 1
        do_prune = _prune_counter % 25 == 0
    if do_prune:
        prune()


def append_error(
    *,
    kind: str,
    name: str,
    detail: str,
    context: str = "",
    duration_ms: float = 0.0,
) -> None:
    append(
        tier="error",
        kind=kind,
        name=name,
        ok=False,
        duration_ms=duration_ms,
        context=context,
        detail=detail,
    )


def append_slow(
    *,
    kind: str,
    name: str,
    duration_ms: float,
    context: str = "",
    detail: str = "",
    ok: bool = True,
) -> None:
    append(
        tier="slow",
        kind=kind,
        name=name,
        ok=ok,
        duration_ms=duration_ms,
        context=context,
        detail=detail or f"{duration_ms:.0f}ms",
    )


def append_info(
    *,
    kind: str,
    name: str,
    duration_ms: float,
    context: str = "",
    detail: str = "",
) -> None:
    """Successful long job (parse/reindex). Not MCP slow, not an error."""
    append(
        tier="info",
        kind=kind,
        name=name,
        ok=True,
        duration_ms=duration_ms,
        context=context,
        detail=detail or f"{duration_ms:.0f}ms",
    )


def maybe_slow(
    *,
    kind: str,
    name: str,
    duration_ms: float,
    context: str = "",
    detail: str = "",
    ok: bool = True,
) -> None:
    try:
        from app.services import runtime_settings

        threshold = runtime_settings.get_slow_request_ms()
    except Exception:
        threshold = DEFAULT_SLOW_MS
    if float(duration_ms or 0) >= float(threshold):
        append_slow(
            kind=kind,
            name=name,
            duration_ms=duration_ms,
            context=context,
            detail=detail,
            ok=ok,
        )


def prune() -> dict[str, int]:
    """Delete by age and max_rows. Returns counts deleted."""
    try:
        from app.services import runtime_settings

        retain_days = runtime_settings.get_app_log_retain_days()
        max_rows = runtime_settings.get_app_log_max_rows()
    except Exception:
        retain_days = DEFAULT_RETAIN_DAYS
        max_rows = DEFAULT_MAX_ROWS

    deleted_age = 0
    deleted_cap = 0
    cutoff = time.time() - retain_days * 86400
    try:
        conn = connect(settings.db_path)
        try:
            cur = conn.execute("DELETE FROM app_log WHERE ts < ?", (cutoff,))
            deleted_age = int(cur.rowcount or 0)
            row = conn.execute("SELECT COUNT(*) AS n FROM app_log").fetchone()
            n = int(row["n"] if row else 0)
            if n > max_rows:
                excess = n - max_rows
                cur = conn.execute(
                    """
                    DELETE FROM app_log WHERE id IN (
                      SELECT id FROM app_log ORDER BY ts ASC, id ASC LIMIT ?
                    )
                    """,
                    (excess,),
                )
                deleted_cap = int(cur.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.debug("app_log prune failed: %s", exc)
    return {"deleted_age": deleted_age, "deleted_cap": deleted_cap}


def count(*, tier: str | None = None) -> int:
    tier_f = (tier or "").strip().lower()
    try:
        conn = connect(settings.db_path)
        try:
            if tier_f in PERSIST_TIERS:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM app_log WHERE tier=?",
                    (tier_f,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS n FROM app_log").fetchone()
            return int(row["n"] if row else 0)
        finally:
            conn.close()
    except Exception:
        return 0


def counts_by_tier(*, since_ts: float | None = None) -> dict[str, int]:
    """Return {error, slow, info, total} counts. Ticker uses error/slow only.

    If ``since_ts`` is set, only rows with ``ts >= since_ts`` are counted
    (live ticker: last N minutes).
    """
    out = {"error": 0, "slow": 0, "info": 0, "total": 0}
    try:
        conn = connect(settings.db_path)
        try:
            if since_ts is not None:
                rows = conn.execute(
                    """
                    SELECT tier, COUNT(*) AS n FROM app_log
                    WHERE ts >= ?
                    GROUP BY tier
                    """,
                    (float(since_ts),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT tier, COUNT(*) AS n FROM app_log GROUP BY tier"
                ).fetchall()
            for row in rows:
                tier = str(row["tier"] or "").strip().lower()
                n = int(row["n"] or 0)
                if tier in PERSIST_TIERS:
                    out[tier] = n
                out["total"] += n
            return out
        finally:
            conn.close()
    except Exception:
        return out


def list_entries(
    *,
    tier: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    tier_f = (tier or "").strip().lower()
    conn = connect(settings.db_path)
    try:
        if tier_f in PERSIST_TIERS:
            rows = conn.execute(
                """
                SELECT id, ts, tier, kind, name, ok, duration_ms, context, detail
                FROM app_log
                WHERE tier=?
                ORDER BY ts DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (tier_f, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, ts, tier, kind, name, ok, duration_ms, context, detail
                FROM app_log
                ORDER BY ts DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear(*, tier: str | None = None) -> int:
    tier_f = (tier or "").strip().lower()
    conn = connect(settings.db_path)
    try:
        if tier_f in PERSIST_TIERS:
            cur = conn.execute("DELETE FROM app_log WHERE tier=?", (tier_f,))
        else:
            cur = conn.execute("DELETE FROM app_log")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def export_text(*, limit: int = 5000) -> str:
    """Block export so multiline args JSON stays readable offline."""
    from datetime import datetime, timezone

    rows = list_entries(limit=limit, offset=0)
    rows = list(reversed(rows))
    lines: list[str] = [
        f"# cfsmcp2 persisted app_log  rows={len(rows)}  "
        f"exported={datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        "# Each record is a --- block. Copy args= JSON to replay MCP tools.",
    ]
    for r in rows:
        ts = float(r.get("ts") or 0)
        stamp = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        ok = "ok" if r.get("ok") else "ERR"
        ms = float(r.get("duration_ms") or 0)
        lines.append("---")
        lines.append(f"ts={stamp}")
        lines.append(f"tier={r.get('tier')}")
        lines.append(f"kind={r.get('kind')}")
        lines.append(f"name={r.get('name')}")
        lines.append(f"status={ok}")
        lines.append(f"duration_ms={ms:.0f}")
        lines.append(f"context={r.get('context') or ''}")
        lines.append("detail=")
        det = str(r.get("detail") or "")
        lines.append(det if det else "(empty)")
    if rows:
        lines.append("---")
    return "\n".join(lines) + "\n"
