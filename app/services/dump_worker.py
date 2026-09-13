"""Background dump worker (1 slot, separate from jobs.py)."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from app.core.config import settings
from app.core.database import connect
from app.services.dump_identity import INGEST_BLOCKED, INGEST_PENDING, evaluate_ingest_verdict
from app.services.dump_ingest import try_post_dump_ingest
from app.services.dump_store import (
    dump_log_dir,
    entity_context,
    get_profile_row,
    profile_password,
    profile_to_spec,
)
from app.services.onec_dump import (
    _read_out_log,
    _tail_text,
    count_files_in_directory,
    run_dump,
)
from app.services import runtime_settings

log = logging.getLogger("cfsmcp2.dump_worker")

_BOOTSTRAP_NOTE = "auto-bootstrap: полная выгрузка перед инкрементом"


class DumpWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cancel_events: dict[int, threading.Event] = {}
        self._current_run_id: int | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="dump-worker", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            run_id = self._current_run_id
            if run_id is not None:
                ev = self._cancel_events.get(run_id)
                if ev:
                    ev.set()
        if self._thread:
            self._thread.join(timeout=35)

    def cancel_run(self, run_id: int) -> bool:
        conn = connect(settings.db_path)
        try:
            row = conn.execute(
                "SELECT id, state FROM dump_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if not row:
                return False
            state = str(row["state"])
            if state == "queued":
                conn.execute(
                    """
                    UPDATE dump_runs SET state='cancelled', finished_at=datetime('now'),
                      error_reason='отменено', last_activity_at=?
                    WHERE id=?
                    """,
                    (time.time(), run_id),
                )
                conn.commit()
                return True
            if state == "running":
                ev = self._cancel_events.get(run_id)
                if ev:
                    ev.set()
                return True
            return False
        finally:
            conn.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            run_id = self._dequeue()
            if run_id is None:
                time.sleep(0.5)
                continue
            try:
                self._execute_run(run_id)
            except Exception:
                log.exception("dump run %s failed unexpectedly", run_id)
            finally:
                with self._lock:
                    if self._current_run_id == run_id:
                        self._current_run_id = None
                    self._cancel_events.pop(run_id, None)

    def _dequeue(self) -> int | None:
        conn = connect(settings.db_path)
        try:
            row = conn.execute(
                """
                SELECT id FROM dump_runs
                WHERE state='queued'
                ORDER BY queue_order ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            run_id = int(row["id"])
            now = time.time()
            conn.execute(
                """
                UPDATE dump_runs SET state='running', started_at=datetime('now'),
                  last_activity_at=?
                WHERE id=? AND state='queued'
                """,
                (now, run_id),
            )
            if conn.total_changes == 0:
                return None
            conn.commit()
            return run_id
        finally:
            conn.close()

    def _touch(self, conn: sqlite3.Connection, run_id: int) -> None:
        conn.execute(
            "UPDATE dump_runs SET last_activity_at=? WHERE id=?",
            (time.time(), run_id),
        )

    def _execute_run(self, run_id: int) -> None:
        cancel_event = threading.Event()
        with self._lock:
            self._current_run_id = run_id
            self._cancel_events[run_id] = cancel_event

        conn = connect(settings.db_path)
        try:
            run_row = conn.execute("SELECT * FROM dump_runs WHERE id=?", (run_id,)).fetchone()
            if not run_row:
                return
            profile_row = get_profile_row(conn, int(run_row["profile_id"]))
            if not profile_row:
                conn.execute(
                    """
                    UPDATE dump_runs SET state='failed', finished_at=datetime('now'),
                      error_reason='профиль не найден', last_activity_at=?
                    WHERE id=?
                    """,
                    (time.time(), run_id),
                )
                conn.commit()
                return

            platform_path = runtime_settings.get_onec_platform_path()
            pwd = profile_password(profile_row)
            spec = profile_to_spec(profile_row, platform_path=platform_path, password=pwd)
            log_file = dump_log_dir(settings.db_path.parent) / f"{run_id}.log"
            progress_out_log: list[Path] = []

            def _heartbeat() -> None:
                while not cancel_event.is_set():
                    time.sleep(2.0)
                    file_count = count_files_in_directory(spec.out_dir)
                    log_tail = ""
                    if progress_out_log:
                        out_log = progress_out_log[0]
                        if out_log.is_file():
                            try:
                                log_tail = _tail_text(_read_out_log(out_log))
                            except OSError:
                                log.debug("dump log tail read failed", exc_info=True)
                    c = connect(settings.db_path)
                    try:
                        c.execute(
                            """
                            UPDATE dump_runs SET last_activity_at=?, file_count=?, log_tail=?
                            WHERE id=?
                            """,
                            (time.time(), file_count, log_tail, run_id),
                        )
                        c.commit()
                    finally:
                        c.close()

            hb = threading.Thread(target=_heartbeat, daemon=True)
            hb.start()
            try:
                result = run_dump(
                    spec,
                    cancel_event=cancel_event,
                    progress_out_log=progress_out_log,
                )
            finally:
                cancel_event.set()
                hb.join(timeout=3)

            if result.log_path and Path(result.log_path).is_file():
                try:
                    shutil.copy2(result.log_path, log_file)
                except OSError:
                    log.debug("copy dump log failed", exc_info=True)

            profile_row = get_profile_row(conn, int(profile_row["id"])) or profile_row
            submit_parse = bool(int(run_row["submit_parse"] or 0))
            post_action = (profile_row["post_action"] or "").strip().lower()
            wants_create = post_action == "create_entity" and not profile_row["entity_id"]

            entity_name, entity_type = entity_context(
                conn, profile_row["entity_id"]
            )
            ingest = evaluate_ingest_verdict(
                target_type=spec.target_type,
                extension_name=spec.extension_name,
                out_dir=spec.out_dir,
                entity_name=entity_name,
                entity_type=entity_type,
                dump_succeeded=result.success,
            )

            if result.cancelled:
                state = "cancelled"
                reason = result.reason or "отменено"
                ingest_state = ""
            elif result.success:
                state = "ok"
                reason = ingest.message if ingest.ingest_state == "blocked" else ""
                ingest_state = ingest.ingest_state or "pending"
            else:
                state = "failed"
                reason = result.reason or "ошибка выгрузки"
                ingest_state = ""

            bootstrap_note = _BOOTSTRAP_NOTE if result.bootstrap_used else ""

            if state == "ok" and wants_create:
                post = try_post_dump_ingest(
                    conn, profile_row, out_dir=spec.out_dir, submit_parse=submit_parse
                )
                ingest_state = post.ingest_state
                reason = post.error_reason or ""
            elif state == "ok" and ingest_state == INGEST_PENDING:
                post = try_post_dump_ingest(
                    conn, profile_row, out_dir=spec.out_dir, submit_parse=submit_parse
                )
                ingest_state = post.ingest_state
                if post.error_reason:
                    reason = post.error_reason
            elif state == "ok" and ingest_state == INGEST_BLOCKED:
                pass

            conn.execute(
                """
                UPDATE dump_runs SET
                  state=?, finished_at=datetime('now'), return_code=?,
                  error_reason=?, log_path=?, log_tail=?, ingest_state=?,
                  bootstrap_note=?, file_count=?, last_activity_at=?
                WHERE id=?
                """,
                (
                    state,
                    result.return_code,
                    reason,
                    str(log_file),
                    result.log_tail,
                    ingest_state,
                    bootstrap_note,
                    result.file_count,
                    time.time(),
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE dump_profiles SET last_run_state=?, last_run_at=datetime('now'),
                  updated_at=datetime('now')
                WHERE id=?
                """,
                (state, int(profile_row["id"])),
            )
            conn.commit()
        finally:
            conn.close()


_worker: DumpWorker | None = None


def get_worker() -> DumpWorker:
    global _worker
    if _worker is None:
        _worker = DumpWorker()
    return _worker
