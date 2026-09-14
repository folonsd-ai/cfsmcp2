"""PyInstaller runtime hook: log uncaught exceptions to data/crash.log."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _crash_log_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path.cwd()
    return base / "data" / "crash.log"


def _write_crash(text: str) -> None:
    try:
        path = _crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _null_stream():
    import os

    try:
        return open(os.devnull, "w", encoding="utf-8")
    except OSError:
        return None


def _ensure_stdio() -> None:
    if "--server" not in sys.argv:
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = _null_stream()
    if stream is None:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _hook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    _write_crash(text)
    sys.__excepthook__(exc_type, exc, tb)


_ensure_stdio()
sys.excepthook = _hook
