"""Logging: console (verbose) + data/server.log (important only, cleared on server start)."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.core.paths import default_data_dir

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# INFO in server.log only from these loggers (WARNING+ always passes).
_FILE_INFO_LOGGERS = frozenset(
    {
        "cfsmcp2",
        "cfsmcp2.launcher",
        "cfsmcp2.dump_recovery",
        "cfsmcp2.dump_worker",
        "cfsmcp2.jobs",
        "cfsmcp2.system",
        "cfsmcp2.onec_dump",
        "cfsmcp2.onec_platform",
        "cfsmcp2.mounts",
        "cfsmcp2.entities",
        "cfsmcp2.dump_ingest",
        "cfsmcp2.db",
    }
)

_configured = False


class ServerLogFilter(logging.Filter):
    """server.log: WARNING+ always; INFO only from allowlisted app loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno < logging.INFO:
            return False
        return record.name in _FILE_INFO_LOGGERS


def server_log_path() -> Path:
    return default_data_dir() / "server.log"


def clear_server_log() -> Path:
    path = server_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    path.write_text(f"# cfsmcp2 server log started {stamp}\n", encoding="utf-8")
    return path


def _quiet_access_logs() -> None:
    """HTTP access lines — noise in console and server.log."""
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def configure_logging(*, console: bool = True, file: bool = True) -> Path | None:
    """Configure root logging once per process. Clears server.log when file logging is enabled."""
    global _configured
    if _configured:
        return server_log_path() if file else None
    _configured = True

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(LOG_FORMAT)

    if console:
        stream = sys.stderr if sys.stderr is not None else sys.stdout
        if stream is not None:
            sh = logging.StreamHandler(stream)
            sh.setLevel(logging.INFO)
            sh.setFormatter(fmt)
            root.addHandler(sh)

    log_path: Path | None = None
    if file:
        log_path = clear_server_log()
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.addFilter(ServerLogFilter())
        fh.setFormatter(fmt)
        root.addHandler(fh)

    _quiet_access_logs()
    return log_path
