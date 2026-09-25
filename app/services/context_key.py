"""Normalized keys for MCP context and tag names (parallel-alphabet homoglyphs)."""

from __future__ import annotations

import unicodedata

# Latin → Cyrillic (after casefold). No ICU / digits / Greek.
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "a": "а",
        "c": "с",
        "e": "е",
        "h": "н",
        "i": "и",
        "k": "к",
        "m": "м",
        "o": "о",
        "p": "р",
        "t": "т",
        "x": "х",
        "y": "у",
        "і": "и",  # Ukrainian і → same bucket as и
    }
)


def normalize_context_key(raw: str) -> str:
    """NFKC → map Latin homoglyphs to Cyrillic → casefold."""
    s = unicodedata.normalize("NFKC", (raw or "").strip())
    s = s.casefold()
    return s.translate(_LATIN_TO_CYRILLIC)
