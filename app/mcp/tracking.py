"""MCP _track helpers: defer logging, usage context aggregation."""

from __future__ import annotations

import time
from typing import Any

from app.services import context_defer as defer_svc
from app.services.context_errors import is_defer_eligible
from app.services.usage_stats import usage_stats


def effective_context_from_result(result: Any, *, client_ok: bool) -> str:
    if not client_ok or not isinstance(result, dict):
        return ""
    return str(result.get("effective_context") or result.get("context") or "").strip()


def handle_mcp_track_finally(
    *,
    tool_name: str,
    context_arg: str,
    result: Any,
    client_ok: bool,
    detail: str,
    duration_ms: float,
    replay: str,
    for_search: bool,
) -> None:
    defer_svc.flush_expired()
    eff = effective_context_from_result(result, client_ok=client_ok)
    if client_ok and eff:
        defer_svc.note_success_context(context_arg, eff)

    defer_eligible = isinstance(result, dict) and is_defer_eligible(result)
    usage_ok = client_ok or defer_eligible

    agg_ctx = eff if client_ok else ""

    if defer_eligible:
        payload = result  # type: ignore[assignment]
        code = str(payload.get("code") or "")
        inp = str(payload.get("input_context") or context_arg or "")
        flushed_replay = defer_svc.on_repeat_input(inp, code, replay)
        if flushed_replay:
            from app.services import app_log as app_log_svc

            app_log_svc.append_error(
                kind="mcp",
                name=tool_name,
                detail=flushed_replay,
                context=context_arg,
                duration_ms=duration_ms,
            )
        else:
            defer_svc.register_defer(
                input_context=inp,
                code=code,
                candidates=list(payload.get("candidates") or []),
                replay=replay,
                tool_name=tool_name,
                context_arg=context_arg,
                for_search=for_search,
            )
        usage_stats.note_context_resolve(code)
    elif isinstance(result, dict) and result.get("code"):
        usage_stats.note_context_resolve(str(result.get("code")))
    elif client_ok and isinstance(result, dict) and result.get("matched_by"):
        usage_stats.note_context_resolve(str(result.get("matched_by")))

    usage_stats.record(
        kind="mcp",
        name=tool_name,
        ok=usage_ok,
        duration_ms=duration_ms,
        context=agg_ctx,
        detail=detail[:200],
        tier="usage",
        persist=False,
    )

    for slot in defer_svc.flush_expired():
        from app.services import app_log as app_log_svc

        app_log_svc.append_error(
            kind="mcp",
            name=slot.tool_name,
            detail=slot.replay,
            context=slot.context_arg,
            duration_ms=duration_ms,
        )
