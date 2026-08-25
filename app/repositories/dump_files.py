from __future__ import annotations

import sqlite3
from typing import Iterable


def load_stamps(conn: sqlite3.Connection, entity_id: int) -> dict[str, tuple[int, int]]:
    """rel_path -> (mtime_ns, size) from the last successful dump/report parse."""
    rows = conn.execute(
        "SELECT rel_path, mtime_ns, size FROM dump_file_state WHERE entity_id=?",
        (entity_id,),
    ).fetchall()
    return {str(r["rel_path"]): (int(r["mtime_ns"] or 0), int(r["size"] or 0)) for r in rows}


def replace_stamps(
    conn: sqlite3.Connection,
    entity_id: int,
    stamps: dict[str, tuple[int, int]],
) -> None:
    """Replace the file snapshot for an entity (seen files this parse)."""
    conn.execute("DELETE FROM dump_file_state WHERE entity_id=?", (entity_id,))
    if not stamps:
        return
    conn.executemany(
        """
        INSERT INTO dump_file_state(entity_id, rel_path, mtime_ns, size)
        VALUES (?,?,?,?)
        """,
        [(entity_id, rel, int(mtime_ns), int(size)) for rel, (mtime_ns, size) in stamps.items()],
    )


def copy_stamps(conn: sqlite3.Connection, source_id: int, dest_id: int) -> None:
    conn.execute(
        """
        INSERT INTO dump_file_state(entity_id, rel_path, mtime_ns, size)
        SELECT ?, rel_path, mtime_ns, size FROM dump_file_state WHERE entity_id=?
        """,
        (dest_id, source_id),
    )


def classify_counts(rels: Iterable[str]) -> dict[str, int]:
    """Rough phase totals from stored rels (refresh progress without a second walk)."""
    out = {"meta": 0, "assets": 0, "help": 0, "bsl": 0}
    for rel in rels:
        out[classify_rel(rel)] += 1
    return out


def classify_rel(rel: str) -> str:
    lower = (rel or "").replace("\\", "/").lower()
    if "/help/" in lower and lower.endswith((".html", ".htm")):
        return "help"
    if lower.endswith(".bsl") or lower.endswith("/ext/form.bin"):
        return "bsl"
    if lower.endswith("/ext/form.xml") or lower.endswith("/ext/template.xml"):
        return "assets"
    return "meta"
