# -*- coding: utf-8 -*-
"""Per-tool wall-clock budget for MCP search tools."""
from __future__ import annotations

import time


class ToolBudgetExceeded(Exception):
    def __init__(self, reason: str = "tool_budget"):
        self.reason = reason
        super().__init__(reason)


class ToolBudget:
    """Optional deadline for one MCP tool invocation."""

    __slots__ = ("deadline", "degraded", "degraded_reason", "elapsed_ms")

    def __init__(self, budget_ms: float | None = None) -> None:
        ms = float(budget_ms or 0)
        self.deadline = time.perf_counter() + ms / 1000.0 if ms > 0 else None
        self.degraded = False
        self.degraded_reason = ""
        self.elapsed_ms = 0.0

    def exhausted(self) -> bool:
        if self.deadline is None:
            return False
        return time.perf_counter() >= self.deadline

    def remaining_ms(self) -> float:
        if self.deadline is None:
            return float("inf")
        return max(0.0, (self.deadline - time.perf_counter()) * 1000.0)

    def mark_degraded(self, reason: str) -> None:
        self.degraded = True
        self.degraded_reason = reason or "tool_budget"

    def attach(self, payload: dict) -> None:
        if self.degraded:
            payload["degraded"] = True
            payload["degraded_reason"] = self.degraded_reason or "tool_budget"
        if self.elapsed_ms > 0:
            payload["budget_elapsed_ms"] = int(round(self.elapsed_ms))
        payload.setdefault("budget_preliminary", True)
