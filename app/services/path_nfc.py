"""Unicode path normalization for dump / BSL (macOS NFD vs 1C NFC).

APFS/HFS store filenames as NFD (e.g. «й» = и + combining breve). Metadata
``Name`` in XML and report paths are NFC. Without NFC, folder segments from
``Path.relative_to`` / zip members do not match ``objects.path``.
"""

from __future__ import annotations

import unicodedata


def nfc(value: str | None) -> str:
    """Normalize to NFC; empty for None."""
    if not value:
        return ""
    return unicodedata.normalize("NFC", value)


def nfc_rel(path: str | None) -> str:
    """NFC + ``\\`` → ``/`` for dump-relative paths."""
    s = nfc(path).replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return s.lstrip("/")
