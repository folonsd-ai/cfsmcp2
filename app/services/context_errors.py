from __future__ import annotations

from typing import Any

CONTEXT_CODES = frozenset(
    {"unknown", "ambiguous", "invariant_violation", "tag_not_allowed_on_get"}
)
DEFER_CODES = frozenset({"unknown", "ambiguous", "tag_not_allowed_on_get"})


class ContextResolveError(Exception):
    """Structured MCP context resolution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        input_context: str = "",
        candidates: list[dict[str, Any]] | None = None,
        next_step: str = "",
    ) -> None:
        if code not in CONTEXT_CODES:
            raise ValueError(f"invalid context error code: {code}")
        self.code = code
        self.message = message
        self.input_context = input_context or ""
        self.candidates = list(candidates or [])
        self.next_step = next_step or ""
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "error": self.message,
            "code": self.code,
            "input_context": self.input_context,
            "candidates": self.candidates,
        }
        if self.next_step:
            out["next_step"] = self.next_step
        return out


def is_defer_eligible(payload: dict[str, Any] | None) -> bool:
    if not payload or not payload.get("error"):
        return False
    code = payload.get("code")
    if code not in DEFER_CODES:
        return False
    cands = payload.get("candidates")
    return isinstance(cands, list) and len(cands) > 0


def mcp_error_dict(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ContextResolveError):
        return exc.to_dict()
    return {"error": str(exc)}


def attach_resolution_fields(
    out: dict[str, Any],
    *,
    effective_context: str,
    matched_by: str,
    input_context: str | None = None,
) -> dict[str, Any]:
    out["context"] = effective_context
    out["effective_context"] = effective_context
    out["matched_by"] = matched_by
    if input_context and input_context != effective_context:
        out["input_context"] = input_context
        out["next_step"] = "Copy effective_context into the next tool call; do not retype."
    if "contexts" in out:
        out["context_count"] = len(out.get("contexts") or [])
    return out
