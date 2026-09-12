from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("cfsmcp2.jobs")


@dataclass
class JobItem:
    job_id: str
    entity_id: int | None
    kind: str
    fn: Callable[..., Any]
    args: tuple[Any, ...] = field(default_factory=tuple)
    kwargs: dict[str, Any] = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.time)


_lock = threading.Lock()
_wake = threading.Event()
_pending: list[JobItem] = []
_paused: list[JobItem] = []
_running: JobItem | None = None
_worker_started = False
_cancelled: set[int] = set()


def _copy_job(job: JobItem) -> JobItem:
    return JobItem(
        job_id=job.job_id,
        entity_id=job.entity_id,
        kind=job.kind,
        fn=job.fn,
        args=job.args,
        kwargs=dict(job.kwargs),
        submitted_at=job.submitted_at,
    )


def is_cancelled(entity_id: int) -> bool:
    with _lock:
        return int(entity_id) in _cancelled


def is_paused(entity_id: int) -> bool:
    eid = int(entity_id)
    with _lock:
        return any(j.entity_id == eid for j in _paused)


def cancel_entity(entity_id: int) -> None:
    """Stop in-flight work and drop queued slots for this entity (delete path)."""
    eid = int(entity_id)
    with _lock:
        _cancelled.add(eid)
        _pending[:] = [j for j in _pending if j.entity_id != eid]
        _paused[:] = [j for j in _paused if j.entity_id != eid]
    _wake.set()
    log.info("cancelled background jobs entity=%s", eid)


def pause_job(job_id: str) -> JobItem | None:
    """Pause a queued or running job; keep it in the paused list for resume."""
    jid = str(job_id or "").strip()
    if not jid:
        return None
    paused: JobItem | None = None
    entity_id: int | None = None
    is_running = False
    with _lock:
        if _running is not None and _running.job_id == jid:
            paused = _copy_job(_running)
            entity_id = _running.entity_id
            is_running = True
            _paused.append(paused)
        else:
            for i, job in enumerate(_pending):
                if job.job_id == jid:
                    paused = _pending.pop(i)
                    _paused.append(paused)
                    entity_id = paused.entity_id
                    _wake.set()
                    log.info(
                        "paused pending job job_id=%s entity=%s fn=%s",
                        jid,
                        entity_id,
                        paused.kind,
                    )
                    return paused
            for job in _paused:
                if job.job_id == jid:
                    return job
            return None
    if is_running and entity_id is not None:
        with _lock:
            _cancelled.add(int(entity_id))
        _wake.set()
        log.info(
            "paused running job job_id=%s entity=%s fn=%s",
            jid,
            entity_id,
            paused.kind if paused else "",
        )
    return paused


def _prepare_job_for_resume(job: JobItem) -> JobItem:
    """Ensure reindex jobs resume from partial progress after pause."""
    if job.kind != "reindex_entity":
        return job
    out = _copy_job(job)
    out.kwargs = dict(job.kwargs)
    out.kwargs["resume"] = True
    return out


def resume_job(job_id: str) -> JobItem | None:
    """Move a paused job back to the tail of the active queue."""
    jid = str(job_id or "").strip()
    if not jid:
        return None
    with _lock:
        for i, job in enumerate(_paused):
            if job.job_id != jid:
                continue
            job = _prepare_job_for_resume(_paused.pop(i))
            eid = job.entity_id
            if eid is not None:
                _cancelled.discard(int(eid))
                _pending[:] = [j for j in _pending if j.entity_id != eid]
            _pending.append(job)
            log.info(
                "resumed paused job job_id=%s entity=%s fn=%s",
                jid,
                eid,
                job.kind,
            )
            _ensure_worker_locked()
            _wake.set()
            return job
    return None


def cancel_job(job_id: str) -> bool:
    """Backward-compatible alias: pause, not discard."""
    return pause_job(job_id) is not None


def _finish_entity_job(entity_id: int) -> None:
    eid = int(entity_id)
    with _lock:
        _cancelled.discard(eid)


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        _ensure_worker_locked()


def _ensure_worker_locked() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, name="cfsmcp2-queue", daemon=True).start()


def _worker_loop() -> None:
    while True:
        _wake.wait(timeout=0.5)
        _wake.clear()
        while True:
            job = _dequeue_next()
            if job is None:
                break
            _run_job(job)


def _dequeue_next() -> JobItem | None:
    global _running
    with _lock:
        if _running is not None or not _pending:
            return None
        while _pending:
            job = _pending.pop(0)
            eid = job.entity_id
            if eid is not None and eid in _cancelled:
                log.info("job skipped (cancelled) entity=%s fn=%s", eid, job.kind)
                continue
            _running = job
            return job
    return None


def _run_job(job: JobItem) -> None:
    global _running
    entity_id = job.entity_id
    try:
        if entity_id is not None and is_cancelled(entity_id):
            log.info("job skipped (cancelled) entity=%s fn=%s", entity_id, job.kind)
            return
        job.fn(*job.args, **job.kwargs)
    except Exception:
        log.exception("background job failed entity=%s fn=%s", entity_id, job.kind)
    finally:
        with _lock:
            _running = None
        if entity_id is not None:
            _finish_entity_job(entity_id)
        _wake.set()


def submit(fn, *args, **kwargs):
    entity_id = int(args[0]) if args and isinstance(args[0], int) else None
    kind = str(getattr(fn, "__name__", "") or "job")
    with _lock:
        if entity_id is not None:
            if _running is not None and _running.entity_id == entity_id:
                log.info(
                    "job skipped, already running entity=%s fn=%s",
                    entity_id,
                    kind,
                )
                return _running
            _pending[:] = [j for j in _pending if j.entity_id != entity_id]
            _paused[:] = [j for j in _paused if j.entity_id != entity_id]
            _cancelled.discard(entity_id)
    job = JobItem(
        job_id=uuid.uuid4().hex[:12],
        entity_id=entity_id,
        kind=kind,
        fn=fn,
        args=args,
        kwargs=kwargs,
    )
    with _lock:
        _pending.append(job)
        pos = len(_pending) + len(_paused) + (1 if _running else 0)
    log.info(
        "queued job entity=%s fn=%s job_id=%s position=%s",
        entity_id,
        job.kind,
        job.job_id,
        pos,
    )
    _ensure_worker()
    _wake.set()
    return job


def reorder_pending(job_ids: list[str]) -> None:
    """Reorder active pending jobs; running and paused jobs stay in place."""
    ids = [str(j).strip() for j in job_ids if str(j).strip()]
    with _lock:
        pending_map = {j.job_id: j for j in _pending}
        if len(ids) != len(_pending):
            raise ValueError("job_ids length must match pending queue")
        if set(ids) != set(pending_map):
            raise ValueError("job_ids must list every pending job exactly once")
        _pending[:] = [pending_map[jid] for jid in ids]
    log.info("reordered job queue pending=%s", len(ids))


def queue_snapshot() -> dict[str, Any]:
    with _lock:
        running = _job_view(_running, position=0, state="running") if _running else None
        base = 1 if _running else 0
        pending = [
            _job_view(job, position=i + 1 + base, state="pending")
            for i, job in enumerate(_pending)
        ]
        paused = [
            _job_view(
                job,
                position=i + 1 + base + len(_pending),
                state="paused",
            )
            for i, job in enumerate(_paused)
        ]
        return {
            "running": running,
            "pending": pending,
            "paused": paused,
            "pending_count": len(pending),
            "paused_count": len(paused),
            "total_count": len(pending) + len(paused) + (1 if _running else 0),
        }


def _job_view(job: JobItem | None, *, position: int, state: str) -> dict[str, Any]:
    assert job is not None
    return {
        "job_id": job.job_id,
        "entity_id": job.entity_id,
        "kind": job.kind,
        "state": state,
        "position": int(position),
        "submitted_at": float(job.submitted_at),
    }


def enrich_queue_names(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach entity name/status from SQLite for UI."""
    ids: set[int] = set()
    for key in ("running",):
        row = snapshot.get(key)
        if row and row.get("entity_id") is not None:
            ids.add(int(row["entity_id"]))
    for key in ("pending", "paused"):
        for row in snapshot.get(key) or []:
            if row.get("entity_id") is not None:
                ids.add(int(row["entity_id"]))
    names: dict[int, dict[str, str]] = {}
    if ids:
        try:
            from app.core.config import settings
            from app.core.database import connect

            conn = connect(settings.db_path)
            try:
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT id, name, status FROM entities WHERE id IN ({placeholders})",
                    tuple(sorted(ids)),
                ).fetchall()
                for r in rows:
                    names[int(r["id"])] = {
                        "name": str(r["name"] or ""),
                        "status": str(r["status"] or ""),
                    }
            finally:
                conn.close()
        except Exception as exc:
            log.debug("queue name enrich failed: %s", exc)

    def _attach(row: dict[str, Any]) -> dict[str, Any]:
        meta = names.get(int(row.get("entity_id") or 0), {})
        return {
            **row,
            "entity_name": meta.get("name") or "",
            "entity_status": meta.get("status") or "",
        }

    out = dict(snapshot)
    running = snapshot.get("running")
    out["running"] = _attach(running) if running else None
    out["pending"] = [_attach(row) for row in (snapshot.get("pending") or [])]
    out["paused"] = [_attach(row) for row in (snapshot.get("paused") or [])]
    return out
