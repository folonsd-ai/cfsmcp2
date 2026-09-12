"""Runtime paths: dev/Docker vs Windows portable (PyInstaller)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_portable_runtime() -> bool:
    """True for PyInstaller bundle or explicit CFSMCP2_PORTABLE=1 (local smoke)."""
    if is_frozen():
        return True
    return os.environ.get("CFSMCP2_PORTABLE", "").strip().lower() in ("1", "true", "yes")


def portable_exe_dir() -> Path:
    """Directory containing cfsmcp2.exe (portable root)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    override = os.environ.get("CFSMCP2_EXE_DIR", "").strip()
    if override:
        return Path(override).resolve()
    return Path.cwd()


def default_data_dir() -> Path:
    if is_portable_runtime():
        return portable_exe_dir() / "data"
    return Path("./data")


def resolve_static_dir() -> Path:
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "app" / "static"
    return Path(__file__).resolve().parent.parent / "static"
