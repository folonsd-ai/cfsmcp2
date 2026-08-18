# -*- coding: utf-8 -*-
"""Lightweight phase timing for search / MCP diagnostics."""
from __future__ import annotations

import time
from typing import Any


class PhaseTimer:
    """Accumulate wall-time per named phase; produce timing_ms + bottleneck."""

    def __init__(self) -> None:
        self._phases: dict[str, float] = {}
        self._t0 = time.perf_counter()
        self._mark = self._t0

    def add(self, name: str, seconds: float) -> None:
        if not name or seconds < 0:
            return
        self._phases[name] = self._phases.get(name, 0.0) + float(seconds)

    def span(self, name: str):
        """Context manager: time a block under ``name``."""
        timer = self

        class _Span:
            def __enter__(self_inner):
                self_inner._a = time.perf_counter()
                return self_inner

            def __exit__(self_inner, *exc):
                timer.add(name, time.perf_counter() - self_inner._a)
                return False

        return _Span()

    def lap(self, name: str) -> float:
        """Time since last lap/mark; accumulate under ``name``."""
        now = time.perf_counter()
        dt = now - self._mark
        self._mark = now
        self.add(name, dt)
        return dt

    def snapshot(
        self,
        *,
        scale: dict[str, Any] | None = None,
        hint: str = "",
    ) -> dict[str, Any]:
        timing_ms = {k: int(round(v * 1000)) for k, v in self._phases.items() if v > 0}
        total = int(round((time.perf_counter() - self._t0) * 1000))
        timing_ms["total"] = total
        bottleneck = ""
        if self._phases:
            bottleneck = max(self._phases.items(), key=lambda kv: kv[1])[0]
        out: dict[str, Any] = {
            "timing_ms": timing_ms,
            "bottleneck": bottleneck,
        }
        if scale:
            out["scale"] = scale
        if hint:
            out["hint"] = hint
        return out


def attach_timing(payload: dict, timer: PhaseTimer, **kwargs: Any) -> dict:
    """Merge timing fields into a result dict (in place + return)."""
    snap = timer.snapshot(**kwargs)
    payload["timing_ms"] = snap["timing_ms"]
    payload["bottleneck"] = snap.get("bottleneck") or ""
    if "scale" in snap:
        payload["scale"] = snap["scale"]
    if snap.get("hint"):
        payload["hint"] = snap["hint"]
    return payload


def bottleneck_hint(bottleneck: str, *, match_mode: str = "", expand: str = "") -> str:
    """Short human hint for logs (not a substitute for timing_ms)."""
    b = (bottleneck or "").strip()
    hints = {
        "embed": "LM Studio embedding of the query",
        "zvec": "hybrid vector+FTS query in zvec",
        "sql_recall": "SQL stem/howto/report recall after hybrid",
        "rank": "rerank / merge hits",
        "exact": "SQLite exact method name scan",
        "fuzzy": "semantic fallback for methods (embed+zvec)",
        "resolve": "resolve context / object path",
        "sql_links": "load incoming/outgoing links from SQLite",
        "expand_paths": "paginate/expand full usage or impact paths",
        "structure": "load object card / children / props",
        "bfs": "BFS over link graph (trace_impact)",
        "open_coll": "open zvec collection",
        "fts": "SQLite FTS5 lexical search",
    }
    base = hints.get(b, b or "unknown")
    extra: list[str] = []
    if match_mode == "fuzzy":
        extra.append("match_mode=fuzzy")
    if expand == "paths":
        extra.append("expand=paths")
    if extra:
        return f"{base} ({', '.join(extra)})"
    return base
