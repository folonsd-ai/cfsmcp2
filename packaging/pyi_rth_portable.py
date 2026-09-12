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


def _server_log_stream():
    import os

    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        return stream
    for fd in (1, 2):
        try:
            return os.fdopen(fd, "a", closefd=False)
        except OSError:
            continue
    log_path = _crash_log_path().parent / "server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(log_path, "a", encoding="utf-8")
    except OSError:
        return None


def _ensure_stdio() -> None:
    if "--server" not in sys.argv:
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = _server_log_stream()
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
