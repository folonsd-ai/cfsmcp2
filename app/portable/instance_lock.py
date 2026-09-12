"""Single-instance lock: one GUI launcher per portable install directory."""

from __future__ import annotations

import os
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LauncherInstanceLock:
    def __init__(self, root_dir: Path) -> None:
        self.lock_path = root_dir / "data" / ".launcher.lock"
        self._fh = None

    def _lock_holder_pid(self) -> int | None:
        try:
            if self.lock_path.is_file():
                text = self.lock_path.read_text(encoding="utf-8").strip()
                if text.isdigit():
                    return int(text)
        except OSError:
            pass
        return None

    def _try_acquire(self) -> bool:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.lock_path, "a+")
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            return False

    def acquire(self) -> bool:
        if self._try_acquire():
            return True
        holder = self._lock_holder_pid()
        if holder is not None and not _pid_alive(holder):
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            return self._try_acquire()
        return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        except OSError:
            pass
        self._fh = None
