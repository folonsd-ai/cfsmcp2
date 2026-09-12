from __future__ import annotations

import gc
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import zvec

from app.core.config import settings

log = logging.getLogger("cfsmcp2.zvec_store")


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _prewarm_globs() -> tuple[str, ...]:
    return (
        "**/embedding.index*.proxima",
        "**/scalar.*.ipc",
    )


def _embedding_index_bytes(root: Path) -> int:
    total = 0
    for p in root.glob("**/embedding.index*.proxima"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _iter_prewarm_files(root: Path) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for pattern in _prewarm_globs():
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            k = _key(p)
            if k in seen:
                continue
            seen.add(k)
            if "fts" in p.parts or p.suffix.lower() == ".sst":
                continue
            out.append(p)
    out.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    return out


class ZvecStore:
    """Process-wide cache of open zvec collections (one handle per path)."""

    def __init__(self) -> None:
        self._meta_lock = threading.Lock()
        self._col_locks: dict[str, threading.RLock] = {}
        # key -> (collection, read_only)
        self._collections: dict[str, tuple[Any, bool]] = {}
        self._prewarm_started: set[str] = set()
        self._prewarm_stats: dict[str, dict[str, Any]] = {}
        self._metrics_lock = threading.Lock()
        self._last_open_metrics: dict[str, float | int | bool] = {}

    def _col_lock(self, key: str) -> threading.RLock:
        with self._meta_lock:
            lock = self._col_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._col_locks[key] = lock
            return lock

    def take_open_metrics(self) -> dict[str, float | int | bool]:
        with self._metrics_lock:
            out = dict(self._last_open_metrics)
            self._last_open_metrics.clear()
        return out

    def snapshot_prewarm(self, path: Path | str) -> dict[str, Any]:
        """Last prewarm stats for a collection (for search scale diagnostics)."""
        key = _key(path)
        with self._meta_lock:
            raw = dict(self._prewarm_stats.get(key) or {})
        if not raw:
            return {}
        out: dict[str, Any] = {}
        if raw.get("prewarm_bytes") is not None:
            out["prewarm_bytes"] = int(raw["prewarm_bytes"])
        if raw.get("prewarm_planned_bytes") is not None:
            out["prewarm_planned_bytes"] = int(raw["prewarm_planned_bytes"])
        if raw.get("embedding_index_bytes") is not None:
            out["embedding_index_bytes"] = int(raw["embedding_index_bytes"])
        if raw.get("prewarm_done") is not None:
            out["prewarm_done"] = bool(raw["prewarm_done"])
        if raw.get("prewarm_ms") is not None:
            out["prewarm_ms"] = int(raw["prewarm_ms"])
        return out

    def _set_open_metrics(self, **kwargs: float | int | bool) -> None:
        with self._metrics_lock:
            self._last_open_metrics.update(kwargs)

    def _acquire_zvec_wait(self, wait_ms: float) -> None:
        if wait_ms <= 0.05:
            return
        try:
            from app.services import mcp_busy

            mcp_busy.add_zvec_wait(wait_ms)
        except Exception:
            pass

    def _clear_waiting_flag(self) -> None:
        try:
            from app.services import mcp_busy

            mcp_busy.clear_waiting_flag()
        except Exception:
            pass

    def get(self, path: Path | str, *, read_only: bool = True) -> Any:
        p = Path(path)
        key = _key(p)
        t0 = time.perf_counter()
        cached = self._collections.get(key)
        if cached is not None:
            coll, ro = cached
            if read_only or not ro:
                wait_ms = (time.perf_counter() - t0) * 1000.0
                self._set_open_metrics(
                    lock_wait_ms=wait_ms,
                    load_ms=0.0,
                    cache_hit=True,
                )
                return coll

        col_lock = self._col_lock(key)
        lock_wait_start = time.perf_counter()
        col_lock.acquire()
        lock_wait_ms = (time.perf_counter() - lock_wait_start) * 1000.0
        self._acquire_zvec_wait(lock_wait_ms)
        load_ms = 0.0
        try:
            cached = self._collections.get(key)
            if cached is not None:
                coll, ro = cached
                if read_only or not ro:
                    self._set_open_metrics(
                        lock_wait_ms=lock_wait_ms,
                        load_ms=0.0,
                        cache_hit=True,
                    )
                    return coll
                self._close_unlocked(key)

            if not p.exists():
                raise FileNotFoundError(f"zvec collection not found: {p}")

            load_start = time.perf_counter()
            option = zvec.CollectionOption(
                read_only=read_only,
                enable_mmap=bool(settings.zvec_enable_mmap),
            )
            try:
                coll = zvec.open(str(p), option=option)
            except Exception:
                self._close_unlocked(key)
                coll = zvec.open(str(p), option=option)
            load_ms = (time.perf_counter() - load_start) * 1000.0
            self._collections[key] = (coll, read_only)
            self._set_open_metrics(
                lock_wait_ms=lock_wait_ms,
                load_ms=load_ms,
                cache_hit=False,
            )
            log.debug(
                "opened zvec collection %s read_only=%s mmap=%s load_ms=%.0f",
                key,
                read_only,
                settings.zvec_enable_mmap,
                load_ms,
            )
            if settings.zvec_prewarm_enabled:
                self._maybe_prewarm(key, p)
            return coll
        finally:
            self._clear_waiting_flag()
            col_lock.release()

    def _maybe_prewarm(self, key: str, root: Path) -> None:
        with self._meta_lock:
            if key in self._prewarm_started:
                return
            self._prewarm_started.add(key)

        block = max(1, int(settings.zvec_prewarm_block_mb or 8)) * 1024 * 1024
        emb_bytes = _embedding_index_bytes(root)
        planned = sum(fp.stat().st_size for fp in _iter_prewarm_files(root) if fp.exists())
        with self._meta_lock:
            self._prewarm_stats[key] = {
                "prewarm_done": False,
                "prewarm_bytes": 0,
                "prewarm_planned_bytes": planned,
                "embedding_index_bytes": emb_bytes,
                "prewarm_ms": 0,
            }

        def _run() -> None:
            files = _iter_prewarm_files(root)
            if not files:
                return
            t0 = time.perf_counter()
            total = 0
            for fp in files:
                try:
                    with fp.open("rb") as fh:
                        while True:
                            chunk = fh.read(block)
                            if not chunk:
                                break
                            total += len(chunk)
                except OSError as exc:
                    log.debug("prewarm skip %s: %s", fp, exc)
            ms = (time.perf_counter() - t0) * 1000.0
            with self._meta_lock:
                self._prewarm_stats[key] = {
                    "prewarm_done": True,
                    "prewarm_bytes": total,
                    "prewarm_planned_bytes": planned,
                    "embedding_index_bytes": emb_bytes,
                    "prewarm_ms": int(round(ms)),
                }
            log.info(
                "zvec prewarm key=%s files=%d bytes=%d planned=%d emb_index=%d ms=%.0f",
                key,
                len(files),
                total,
                planned,
                emb_bytes,
                ms,
            )

        threading.Thread(
            target=_run,
            name=f"zvec-prewarm-{Path(key).name[:24]}",
            daemon=True,
        ).start()

    def schedule_prewarm(self, path: Path | str) -> None:
        """Re-prewarm after slow zvec (self-heal when page cache was evicted)."""
        if not settings.zvec_prewarm_enabled:
            return
        p = Path(path)
        key = _key(p)
        with self._meta_lock:
            self._prewarm_started.discard(key)
        self._maybe_prewarm(key, p)

    def release(self, path: Path | str) -> None:
        key = _key(path)
        col_lock = self._col_lock(key)
        t0 = time.perf_counter()
        col_lock.acquire()
        wait_ms = (time.perf_counter() - t0) * 1000.0
        self._acquire_zvec_wait(wait_ms)
        try:
            self._close_unlocked(key)
            with self._meta_lock:
                self._prewarm_started.discard(key)
        finally:
            self._clear_waiting_flag()
            col_lock.release()

    def _close_unlocked(self, key: str) -> None:
        cached = self._collections.pop(key, None)
        if cached is None:
            return
        coll, _ro = cached
        try:
            if hasattr(coll, "close"):
                coll.close()
            else:
                try:
                    coll.flush()
                except Exception:
                    pass
                del coll
        except Exception:
            log.debug("close failed for %s", key, exc_info=True)
        gc.collect()
        log.debug("released zvec collection %s", key)


zvec_store = ZvecStore()
