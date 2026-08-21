from __future__ import annotations

import atexit
import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

WINDOW_SEC = 20 * 60  # default; live value from runtime setting stats_window_sec
# Live ticker (top bar) always shows the last 20 minutes, even if the Stats
# tab retention window is shorter — so we retain at least this many seconds.
TICKER_WINDOW_SEC = 20 * 60
INDEX_ROLLUP_SEC = 10.0  # verbose: one indexing summary per this window, not per batch

LogLevel = Literal["minimal", "errors", "verbose"]
LogTier = Literal["usage", "error", "verbose"]

LOG_LEVEL_RANK: dict[str, int] = {
    "minimal": 1,
    "errors": 2,
    "verbose": 3,
}
DEFAULT_LOG_LEVEL: LogLevel = "minimal"
MAX_IDENTICAL_ERROR_LINES = 8
MAX_LOG_LINES = 8000
MAX_DETAIL_CHARS = 4000


def _active_window_sec() -> int:
    try:
        from app.services import runtime_settings

        return int(runtime_settings.get_stats_window_sec())
    except Exception:
        return WINDOW_SEC


def _active_log_level() -> LogLevel:
    try:
        from app.services import runtime_settings

        return runtime_settings.get_log_level()  # type: ignore[return-value]
    except Exception:
        return DEFAULT_LOG_LEVEL


def _level_rank(level: str | None) -> int:
    return LOG_LEVEL_RANK.get((level or DEFAULT_LOG_LEVEL).strip().lower(), 1)


def _fmt_ts(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def format_index_last_object(
    path: str = "",
    kind: str = "",
    name: str = "",
) -> str:
    """Human label: metadata path, or «parent · ProcedureName» for BSL methods."""
    path = (path or "").strip()
    kind = (kind or "").strip()
    name = (name or "").strip()
    if not path and not name:
        return ""
    if kind in {"Procedure", "Function"}:
        parent = path
        marker = ".Методы."
        if marker in path:
            parent = path.split(marker, 1)[0]
        method = name or (path.rsplit(".", 1)[-1] if path else "")
        if parent and method:
            return f"{parent} · {method}"
        return method or parent
    return path or name


def _brief(value: Any, *, limit: int = 800) -> str:
    try:
        if value is None:
            return "null"
        if isinstance(value, (str, int, float, bool)):
            s = str(value)
        elif isinstance(value, dict):
            keys = list(value.keys())
            err = value.get("error")
            if err:
                s = f"error={err!s}; keys={keys[:12]}"
            elif "items" in value and isinstance(value.get("items"), list):
                s = f"items={len(value['items'])}; keys={keys[:12]}"
            elif "results" in value and isinstance(value.get("results"), list):
                s = f"results={len(value['results'])}; keys={keys[:12]}"
            else:
                s = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, list):
            s = f"list[{len(value)}]"
            if value:
                s += f" first={_brief(value[0], limit=200)}"
        else:
            s = repr(value)
    except Exception as exc:
        s = f"<unrepr {type(value).__name__}: {exc}>"
    s = " ".join(str(s).split())
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


@dataclass
class _Event:
    ts: float
    kind: str  # mcp | api | embed | index | lifecycle
    name: str
    ok: bool
    duration_ms: float
    context: str = ""
    detail: str = ""


@dataclass
class _IndexRollup:
    """Accumulates indexing/embed work until flushed as one verbose log line."""

    started: float
    last: float
    objects: int = 0
    passages: int = 0
    batches: int = 0
    cache_hits: int = 0
    errors: int = 0
    duration_ms: float = 0.0
    model: str = ""
    role: str = "index"  # index | query
    last_label: str = ""


@dataclass
class _LogLine:
    ts: float
    level: str  # info | error | debug
    kind: str
    name: str
    ok: bool
    context: str = ""
    detail: str = ""
    repeat: int = 1
    duration_ms: float = 0.0


@dataclass
class UsageStats:
    """In-memory usage + diagnostic log; cleared on process restart."""

    window_sec: int = WINDOW_SEC
    max_events: int = 20000
    max_log_lines: int = MAX_LOG_LINES
    max_identical_errors: int = MAX_IDENTICAL_ERROR_LINES
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    started_at: float = field(default_factory=time.time)
    events: deque[_Event] = field(default_factory=lambda: deque(maxlen=20000))
    log_lines: deque[_LogLine] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    _error_counts: dict[str, int] = field(default_factory=dict)
    _index_rollups: dict[tuple[str, str, str], _IndexRollup] = field(default_factory=dict)
    _stop_registered: bool = False

    def __post_init__(self) -> None:
        self.events = deque(maxlen=self.max_events)
        self.log_lines = deque(maxlen=self.max_log_lines)
        if not self._stop_registered:
            atexit.register(self._on_process_exit)
            self._stop_registered = True

    def effective_window_sec(self) -> int:
        return _active_window_sec()

    def retention_window_sec(self) -> int:
        """How long to keep events in RAM (covers Stats window and ticker)."""
        return max(self.effective_window_sec(), TICKER_WINDOW_SEC)

    def effective_log_level(self) -> LogLevel:
        return _active_log_level()

    def _cutoff(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.retention_window_sec()

    def _prune(self, now: float | None = None) -> None:
        cutoff = self._cutoff(now)
        while self.events and self.events[0].ts < cutoff:
            self.events.popleft()

    def _allows(self, tier: LogTier) -> bool:
        rank = _level_rank(self.effective_log_level())
        need = {"usage": 1, "error": 2, "verbose": 3}[tier]
        return rank >= need

    def _append_log_line(
        self,
        *,
        level: str,
        kind: str,
        name: str,
        ok: bool,
        context: str,
        detail: str,
        dedupe: bool,
        duration_ms: float = 0.0,
    ) -> None:
        detail = (detail or "")[:MAX_DETAIL_CHARS]
        now = time.time()
        ms = float(duration_ms or 0.0)
        if dedupe and not ok:
            fp = f"{kind}|{name}|{context}|{detail}"
            count = self._error_counts.get(fp, 0) + 1
            self._error_counts[fp] = count
            if count > self.max_identical_errors:
                if count == self.max_identical_errors + 1:
                    self.log_lines.append(
                        _LogLine(
                            ts=now,
                            level="error",
                            kind=kind,
                            name=name,
                            ok=False,
                            context=context,
                            detail=(
                                f"[suppressed further identical] seen={count}+ "
                                f"fingerprint={fp[:240]}"
                            ),
                            repeat=count,
                            duration_ms=ms,
                        )
                    )
                return
            line = _LogLine(
                ts=now,
                level=level,
                kind=kind,
                name=name,
                ok=ok,
                context=context,
                detail=detail,
                repeat=count,
                duration_ms=ms,
            )
            self.log_lines.append(line)
            return
        self.log_lines.append(
            _LogLine(
                ts=now,
                level=level,
                kind=kind,
                name=name,
                ok=ok,
                context=context,
                detail=detail,
                repeat=1,
                duration_ms=ms,
            )
        )

    def record(
        self,
        *,
        kind: str,
        name: str,
        ok: bool = True,
        duration_ms: float = 0.0,
        context: str = "",
        detail: str = "",
        tier: LogTier = "usage",
        log_level: str | None = None,
        persist: bool = True,
    ) -> None:
        """Record a usage event and optionally a diagnostic log line.

        tier:
          usage   — always (minimal+): start/stop/load/delete/MCP for main-page charts
          error   — errors level+: richer failure details (deduped)
          verbose — verbose level+: MCP args/results and noisy API noise

        persist — write error/slow rows to SQLite (independent of log_level).
          Callers that also use record_error_detail should pass persist=False on
          the usage-tier failure so the richer error line is stored once.
        """
        ctx = context or ""
        det = (detail or "")[:MAX_DETAIL_CHARS]

        # SQLite persist is independent of in-memory log_level.
        if persist:
            try:
                from app.services import app_log as app_log_svc

                if not ok and tier in ("usage", "error"):
                    app_log_svc.append_error(
                        kind=kind,
                        name=name,
                        detail=det if det else (f"{duration_ms:.0f}ms" if duration_ms else "error"),
                        context=ctx,
                        duration_ms=duration_ms,
                    )
                elif ok and duration_ms:
                    app_log_svc.maybe_slow(
                        kind=kind,
                        name=name,
                        duration_ms=duration_ms,
                        context=ctx,
                        detail=det,
                        ok=True,
                    )
            except Exception:
                pass

        if not self._allows(tier):
            return

        now = time.time()
        ev = _Event(
            ts=now,
            kind=kind,
            name=name,
            ok=ok,
            duration_ms=duration_ms,
            context=ctx,
            detail=det,
        )
        with self._lock:
            self._prune(now)
            self._flush_index_rollups_locked(now, force=False)
            # Usage-tier events feed charts; error/verbose may still add event for recent list
            if tier == "usage" or kind in {"mcp", "lifecycle", "index"} or not ok:
                self.events.append(ev)
            if tier == "usage":
                line_level = "info" if ok else "error"
                self._append_log_line(
                    level=line_level,
                    kind=kind,
                    name=name,
                    ok=ok,
                    context=ctx,
                    detail=det if det else (f"{duration_ms:.0f}ms" if duration_ms else ""),
                    dedupe=not ok,
                    duration_ms=duration_ms,
                )
            elif tier == "error":
                self._append_log_line(
                    level=log_level or "error",
                    kind=kind,
                    name=name,
                    ok=ok,
                    context=ctx,
                    detail=det,
                    dedupe=True,
                    duration_ms=duration_ms,
                )
            else:  # verbose
                self._append_log_line(
                    level=log_level or "debug",
                    kind=kind,
                    name=name,
                    ok=ok,
                    context=ctx,
                    detail=det,
                    dedupe=False,
                    duration_ms=duration_ms,
                )

    def record_lifecycle(self, name: str, detail: str = "") -> None:
        self.record(
            kind="lifecycle",
            name=name,
            ok=True,
            detail=detail,
            tier="usage",
        )

    def record_error_detail(
        self,
        *,
        kind: str,
        name: str,
        detail: str,
        context: str = "",
        duration_ms: float = 0.0,
        persist: bool = True,
    ) -> None:
        """Extra diagnostic payload for level=errors+ (deduped).

        By default persists to SQLite (even when log_level=minimal). Pass
        persist=False when the caller already wrote a richer app_log row.
        """
        if persist:
            try:
                from app.services import app_log as app_log_svc

                app_log_svc.append_error(
                    kind=kind,
                    name=name,
                    detail=detail,
                    context=context,
                    duration_ms=duration_ms,
                )
            except Exception:
                pass
        if not self._allows("error"):
            return
        self.record(
            kind=kind,
            name=name,
            ok=False,
            context=context,
            detail=detail,
            duration_ms=duration_ms,
            tier="error",
            persist=False,
        )

    def record_mcp_verbose(
        self,
        *,
        name: str,
        ok: bool,
        duration_ms: float,
        context: str,
        args: Any,
        result: Any,
        error: str = "",
    ) -> None:
        if not self._allows("verbose"):
            return
        parts = [
            f"args={_brief(args)}",
            f"result={_brief(result)}",
        ]
        if error:
            parts.append(f"error={error[:500]}")
        parts.append(f"ms={duration_ms:.1f}")
        self.record(
            kind="mcp",
            name=f"{name}.trace",
            ok=ok,
            duration_ms=duration_ms,
            context=context,
            detail="; ".join(parts),
            tier="verbose",
            log_level="debug",
            persist=False,
        )

    def note_index_progress(
        self,
        *,
        context: str = "",
        role: str = "index",
        model: str = "",
        objects: int = 0,
        passages: int = 0,
        batches: int = 1,
        cache_hits: int = 0,
        duration_ms: float = 0.0,
        ok: bool = True,
        last_path: str = "",
        last_kind: str = "",
        last_name: str = "",
    ) -> None:
        """Accumulate indexing/embed work; verbose log gets a summary every ~10s."""
        last_label = format_index_last_object(last_path, last_kind, last_name)
        if not ok:
            self.record(
                kind="embed" if role == "query" else "index",
                name="progress",
                ok=False,
                duration_ms=duration_ms,
                context=context,
                detail=(
                    f"role={role} model={model} objects={objects} "
                    f"passages={passages} batches={batches}"
                    + (f" last={last_label}" if last_label else "")
                ),
                tier="error",
            )
        if not self._allows("verbose"):
            return
        now = time.time()
        key = ((context or "").strip(), role or "index", (model or "").strip())
        with self._lock:
            bucket = self._index_rollups.get(key)
            if bucket is None:
                bucket = _IndexRollup(started=now, last=now, model=key[2], role=key[1])
                self._index_rollups[key] = bucket
            bucket.last = now
            bucket.objects += max(0, int(objects))
            bucket.passages += max(0, int(passages))
            bucket.batches += max(0, int(batches))
            bucket.cache_hits += max(0, int(cache_hits))
            bucket.duration_ms += max(0.0, float(duration_ms))
            if last_label:
                bucket.last_label = last_label
            if not ok:
                bucket.errors += 1
            if key[2] and not bucket.model:
                bucket.model = key[2]
            self._flush_index_rollups_locked(now, force=False)

    def flush_index_progress(self) -> None:
        """Write pending indexing summaries (end of reindex / export)."""
        if not self._allows("verbose"):
            with self._lock:
                self._index_rollups.clear()
            return
        now = time.time()
        with self._lock:
            self._flush_index_rollups_locked(now, force=True)

    def _flush_index_rollups_locked(self, now: float, *, force: bool) -> None:
        if not self._index_rollups:
            return
        due: list[tuple[tuple[str, str, str], _IndexRollup]] = []
        for key, bucket in list(self._index_rollups.items()):
            age = now - bucket.started
            idle = now - bucket.last
            if force or age >= INDEX_ROLLUP_SEC or idle >= INDEX_ROLLUP_SEC:
                due.append((key, bucket))
                del self._index_rollups[key]
        for key, bucket in due:
            ctx, role, model = key
            span = max(0.1, bucket.last - bucket.started)
            if bucket.last <= bucket.started:
                span = max(0.1, now - bucket.started)
            parts = [
                f"{span:.1f}s",
                f"objects={bucket.objects}" if bucket.objects else "",
                f"passages={bucket.passages}" if bucket.passages else "",
                f"batches={bucket.batches}",
                f"cache_hits={bucket.cache_hits}" if bucket.cache_hits else "",
                f"embed_ms={bucket.duration_ms:.0f}" if bucket.duration_ms else "",
                f"errors={bucket.errors}" if bucket.errors else "",
                f"model={bucket.model or model}" if (bucket.model or model) else "",
                f"last={bucket.last_label}" if bucket.last_label else "",
            ]
            detail = " ".join(p for p in parts if p)
            self._append_log_line(
                level="info",
                kind="index" if role != "query" else "embed",
                name="progress",
                ok=bucket.errors == 0,
                context=ctx,
                detail=detail,
                dedupe=False,
                duration_ms=bucket.duration_ms,
            )
            self.events.append(
                _Event(
                    ts=bucket.last or now,
                    kind="index" if role != "query" else "embed",
                    name="progress",
                    ok=bucket.errors == 0,
                    duration_ms=bucket.duration_ms,
                    context=ctx,
                    detail=detail[:MAX_DETAIL_CHARS],
                )
            )

    def reset(self) -> None:
        with self._lock:
            self.started_at = time.time()
            self.events.clear()
            self.log_lines.clear()
            self._error_counts.clear()
            self._index_rollups.clear()

    def _on_process_exit(self) -> None:
        try:
            self.flush_index_progress()
            self.record_lifecycle("stop", "process exit")
        except Exception:
            pass

    def export_log_text(self) -> str:
        with self._lock:
            self._flush_index_rollups_locked(time.time(), force=True)
            lines = list(self.log_lines)
            level = self.effective_log_level()
            window = self.effective_window_sec()
            err_counts = dict(self._error_counts)
        header = [
            "=== cfsmcp2 application log ===",
            f"exported_at={_fmt_ts(time.time())}",
            f"log_level={level}",
            f"stats_window_sec={window}",
            f"lines={len(lines)}",
            f"unique_error_fingerprints={len(err_counts)}",
            "",
            "Levels: minimal (start/stop/load/delete/MCP) | "
            "errors (+detailed failures, identical capped) | "
            "verbose (+MCP args/results; indexing summaries every "
            f"{int(INDEX_ROLLUP_SEC)}s, not per batch).",
            "",
        ]
        body: list[str] = []
        for e in lines:
            flag = "OK" if e.ok else "ERR"
            rep = f" x{e.repeat}" if e.repeat > 1 else ""
            ctx = f" ctx={e.context}" if e.context else ""
            ms = f" {e.duration_ms:.0f}ms" if e.duration_ms else ""
            det = f" | {e.detail}" if e.detail else ""
            body.append(
                f"{_fmt_ts(e.ts)} [{e.level}] {e.kind}:{e.name} {flag}{rep}{ms}{ctx}{det}"
            )
        if not body:
            body.append("(empty — no events yet; log lives in memory until reset/restart)")
        # Top repeated errors for AI analysis
        top = sorted(err_counts.items(), key=lambda x: -x[1])[:40]
        if top:
            body.append("")
            body.append("--- error fingerprint counts ---")
            for fp, n in top:
                body.append(f"{n}\t{fp[:500]}")
        body.append("")
        body.append("=== end ===")
        return "\n".join(header + body) + "\n"

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._prune(now)
            events_all = list(self.events)
            log_lines = list(self.log_lines)
            started = self.started_at
            window_sec = self.effective_window_sec()
            log_level = self.effective_log_level()

        stats_cut = now - window_sec
        ticker_cut = now - TICKER_WINDOW_SEC
        events = [ev for ev in events_all if ev.ts >= stats_cut]
        ticker_events = [ev for ev in events_all if ev.ts >= ticker_cut]

        totals: dict[str, int] = defaultdict(int)
        errors: dict[str, int] = defaultdict(int)
        dur_sum: dict[str, float] = defaultdict(float)
        dur_cnt: dict[str, int] = defaultdict(int)
        by_context: dict[str, int] = defaultdict(int)

        for ev in events:
            key = f"{ev.kind}:{ev.name}"
            totals[key] += 1
            if not ev.ok:
                errors[key] += 1
            if ev.duration_ms > 0:
                dur_sum[key] += ev.duration_ms
                dur_cnt[key] += 1
            if ev.context:
                by_context[ev.context] += 1

        by_tool: dict[str, dict[str, Any]] = {}
        for key, count in sorted(totals.items(), key=lambda x: -x[1]):
            kind, name = key.split(":", 1)
            avg = (dur_sum.get(key, 0.0) / dur_cnt[key]) if dur_cnt.get(key) else 0.0
            by_tool[key] = {
                "kind": kind,
                "name": name,
                "calls": count,
                "errors": errors.get(key, 0),
                "avg_ms": round(avg, 1),
            }

        bucket_sec = 60
        window = max(1, window_sec // bucket_sec)
        start_bucket = int(now // bucket_sec) - (window - 1)
        timeline = []
        for i in range(window):
            b = start_bucket + i
            timeline.append(
                {
                    "t": datetime.fromtimestamp(b * bucket_sec, tz=timezone.utc)
                    .astimezone()
                    .strftime("%H:%M"),
                    "ts": b * bucket_sec,
                    "mcp": 0,
                    "api": 0,
                    "embed": 0,
                    "index": 0,
                    "errors": 0,
                }
            )
        idx = {row["ts"]: row for row in timeline}
        for ev in events:
            b = int(ev.ts // bucket_sec) * bucket_sec
            row = idx.get(b)
            if row is None:
                continue
            if ev.kind in row:
                row[ev.kind] += 1
            if not ev.ok:
                row["errors"] += 1

        mcp_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "mcp")
        api_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "api")
        embed_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "embed")
        index_calls = sum(v["calls"] for v in by_tool.values() if v["kind"] == "index")
        err_total = sum(errors.values())

        ticker_mcp = sum(1 for ev in ticker_events if ev.kind == "mcp")
        ticker_err_ram = sum(1 for ev in ticker_events if not ev.ok)

        recent = [
            {
                "ts": datetime.fromtimestamp(e.ts, tz=timezone.utc)
                .astimezone()
                .strftime("%d.%m %H:%M:%S"),
                "kind": e.kind,
                "name": e.name,
                "ok": e.ok,
                "ms": round(e.duration_ms, 1),
                "context": e.context,
                "detail": e.detail,
            }
            for e in list(reversed(events))[:40]
        ]

        recent_log = [
            {
                "ts": datetime.fromtimestamp(e.ts, tz=timezone.utc)
                .astimezone()
                .strftime("%d.%m %H:%M:%S"),
                "level": e.level,
                "kind": e.kind,
                "name": e.name,
                "ok": e.ok,
                "ms": round(e.duration_ms, 1),
                "context": e.context,
                "detail": e.detail,
                "repeat": e.repeat,
            }
            for e in list(reversed(log_lines))[:80]
        ]

        from app.services import app_log as app_log_svc

        app_counts = app_log_svc.counts_by_tier(since_ts=ticker_cut)

        return {
            "started_at": datetime.fromtimestamp(started, tz=timezone.utc)
                .astimezone()
                .isoformat(timespec="seconds"),
            "uptime_sec": int(now - started),
            "window_sec": window_sec,
            "ticker_window_sec": TICKER_WINDOW_SEC,
            "log_level": log_level,
            "ephemeral": True,
            "summary": {
                "total_events": len(events),
                "mcp_calls": mcp_calls,
                "api_calls": api_calls,
                "embed_calls": embed_calls,
                "index_ops": index_calls,
                "errors": err_total,
                "log_lines": len(log_lines),
                # Live ticker (top bar): always last TICKER_WINDOW_SEC
                "ticker_mcp_calls": ticker_mcp,
                "ticker_errors_ram": ticker_err_ram,
                "slow_calls": int(app_counts.get("slow") or 0),
                "app_log_errors": int(app_counts.get("error") or 0),
                "app_log_slow": int(app_counts.get("slow") or 0),
            },
            "by_name": list(by_tool.values()),
            "by_context": [
                {"context": k, "calls": v}
                for k, v in sorted(by_context.items(), key=lambda x: -x[1])[:20]
            ],
            "timeline": timeline,
            "recent": recent,
            "recent_log": recent_log,
        }

    def mcp_usage_by_context(self) -> dict[str, Any]:
        """MCP call counts per context name within the active retention window."""
        now = time.time()
        with self._lock:
            self._prune(now)
            events = list(self.events)
            window_sec = self.effective_window_sec()

        by_name: dict[str, int] = defaultdict(int)
        for ev in events:
            if ev.kind != "mcp":
                continue
            if ev.name.endswith(".trace"):
                continue
            ctx = (ev.context or "").strip()
            if not ctx:
                continue
            by_name[ctx] += 1
        total = sum(by_name.values())
        max_calls = max(by_name.values()) if by_name else 0
        return {
            "by_name": dict(by_name),
            "total": total,
            "max_calls": max_calls,
            "window_sec": window_sec,
        }


usage_stats = UsageStats()


def is_important_api(method: str, path: str) -> bool:
    """Load / delete / reindex / copy — counted at minimal log level."""
    m = (method or "").upper()
    p = path or ""
    if m == "POST" and p in {
        "/api/entities/upload",
        "/api/entities/upload-dump",
        "/api/entities/import-path",
    }:
        return True
    if m == "DELETE" and p.startswith("/api/entities/") and p.count("/") == 3:
        return True
    if m == "POST" and (
        p.endswith("/reindex")
        or p.endswith("/copy")
        or p.endswith("/upload-modules")
        or "/modules" in p
    ):
        return True
    return False


_UI_POLL_API_PATHS = frozenset(
    {
        "/api/entities",
        "/api/tags",
        "/api/health",
        "/api/settings/models",
        "/api/system/runtime",
        "/api/system/browse",
        "/api/system/mcp-busy",
    }
)


def is_ui_poll_api(method: str, path: str) -> bool:
    """UI poll / browse GETs — skip successful hits in verbose app-log."""
    m = (method or "").upper()
    p = (path or "").split("?", 1)[0].rstrip("/") or "/"
    return m == "GET" and p in _UI_POLL_API_PATHS
