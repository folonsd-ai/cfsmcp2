"""Recovery helpers for interrupted dump runs."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("cfsmcp2.dump_recovery")


def recover_orphaned_dump_runs(
    conn: sqlite3.Connection,
    *,
    stale_after_sec: float = 3600.0,
    now: float | None = None,
) -> int:
    """Mark stale ``running`` / ``queued`` dump runs as ``failed`` (design R4)."""
    ts = time.time() if now is None else now
    cutoff = ts - max(0.0, stale_after_sec)
    started_cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))
    cur = conn.execute(
        """
        UPDATE dump_runs
        SET state = 'failed',
            finished_at = datetime('now'),
            error_reason = 'прервано рестартом'
        WHERE state = 'queued'
           OR (
                state = 'running'
                AND (
                    (last_activity_at > 0 AND last_activity_at < ?)
                    OR (
                        last_activity_at = 0
                        AND (started_at = '' OR started_at < ?)
                    )
                )
           )
        """,
        (cutoff, started_cutoff),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def recover_pending_create_entities(conn: sqlite3.Connection) -> int:
    """Create contexts for profiles stuck on ``create_entity`` after a successful dump."""
    from app.services.dump_ingest import try_post_dump_ingest

    rows = conn.execute(
        """
        SELECT * FROM dump_profiles
        WHERE lower(trim(post_action)) = 'create_entity'
          AND entity_id IS NULL
          AND trim(out_dir) != ''
          AND last_run_state = 'ok'
        """
    ).fetchall()
    recovered = 0
    for row in rows:
        out_dir = Path(str(row["out_dir"]))
        if not (out_dir / "Configuration.xml").is_file():
            log.warning(
                "skip create_entity recovery profile=%s: no Configuration.xml in %s",
                row["id"],
                out_dir,
            )
            continue
        result = try_post_dump_ingest(conn, row, out_dir=out_dir)
        run_row = conn.execute(
            """
            SELECT id FROM dump_runs
            WHERE profile_id=? AND state='ok'
            ORDER BY id DESC LIMIT 1
            """,
            (int(row["id"]),),
        ).fetchone()
        if run_row:
            conn.execute(
                """
                UPDATE dump_runs SET ingest_state=?, error_reason=?
                WHERE id=?
                """,
                (result.ingest_state, result.error_reason or "", int(run_row["id"])),
            )
        conn.commit()
        if result.ingest_state == "done":
            recovered += 1
            log.info("recovered create_entity profile=%s", row["id"])
        elif result.error_reason:
            log.warning(
                "create_entity recovery failed profile=%s: %s",
                row["id"],
                result.error_reason,
            )
    return recovered
