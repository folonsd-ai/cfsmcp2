# -*- coding: utf-8 -*-
"""Process I/O counters for search diagnostics (Windows + best-effort elsewhere)."""
from __future__ import annotations

import sys
from typing import Any


def page_fault_count() -> int | None:
    """Return process PageFaultCount if available."""
    try:
        import psutil

        return int(psutil.Process().memory_info().num_page_faults)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return int(counters.PageFaultCount)
        except Exception:
            return None
        return None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux: ru_majflt + ru_minflt approximate page-related faults.
        return int(getattr(usage, "ru_majflt", 0) + getattr(usage, "ru_minflt", 0))
    except Exception:
        return None


class PageFaultSpan:
    """Record page faults before/after a heavy phase."""

    __slots__ = ("before", "after")

    def __init__(self) -> None:
        self.before = page_fault_count()
        self.after: int | None = None

    def finish(self) -> dict[str, Any]:
        self.after = page_fault_count()
        out: dict[str, Any] = {}
        if self.before is not None:
            out["page_fault_before"] = self.before
        if self.after is not None:
            out["page_fault_after"] = self.after
        if self.before is not None and self.after is not None:
            out["page_fault_delta"] = max(0, self.after - self.before)
        return out
