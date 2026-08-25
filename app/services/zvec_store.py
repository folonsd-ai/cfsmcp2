from __future__ import annotations

import gc
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import zvec

log = logging.getLogger("cfsmcp2.zvec_store")


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class ZvecStore:
    """Process-wide cache of open zvec collections (one handle per path)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # key -> (collection, read_only)
        self._collections: dict[str, tuple[Any, bool]] = {}

    def _acquire(self) -> float:
        """Acquire store lock; return wait_ms until grant (0 if uncontended)."""
        t0 = time.perf_counter()
        self._lock.acquire()
        return (time.perf_counter() - t0) * 1000.0

    def _release(self) -> None:
        self._lock.release()

    def get(self, path: Path | str, *, read_only: bool = True) -> Any:
        p = Path(path)
        key = _key(p)
        wait_ms = self._acquire()
        try:
            if wait_ms > 0.05:
                try:
                    from app.services import mcp_busy

                    mcp_busy.add_zvec_wait(wait_ms)
                except Exception:
                    pass
            cached = self._collections.get(key)
            if cached is not None:
                coll, ro = cached
                if read_only or not ro:
                    return coll
                # Need write access but cache is read-only — reopen
                self._close_unlocked(key)
            if not p.exists():
                raise FileNotFoundError(f"zvec collection not found: {p}")
            option = zvec.CollectionOption(read_only=read_only, enable_mmap=True)
            try:
                coll = zvec.open(str(p), option=option)
            except Exception:
                self._close_unlocked(key)
                coll = zvec.open(str(p), option=option)
            self._collections[key] = (coll, read_only)
            log.debug("opened zvec collection %s read_only=%s", key, read_only)
            return coll
        finally:
            try:
                from app.services import mcp_busy

                mcp_busy.clear_waiting_flag()
            except Exception:
                pass
            self._release()

    def release(self, path: Path | str) -> None:
        key = _key(path)
        wait_ms = self._acquire()
        try:
            if wait_ms > 0.05:
                try:
                    from app.services import mcp_busy

                    mcp_busy.add_zvec_wait(wait_ms)
                except Exception:
                    pass
            self._close_unlocked(key)
        finally:
            try:
                from app.services import mcp_busy

                mcp_busy.clear_waiting_flag()
            except Exception:
                pass
            self._release()

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
