from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("cfsmcp2.db")

BUSY_TIMEOUT_MS = 15000
CONNECT_TIMEOUT_SEC = 15.0
LOCKED_RETRIES = 2
LOCKED_RETRY_SLEEP_SEC = 0.25

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  color TEXT NOT NULL DEFAULT '#64748b',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  name_locked INTEGER NOT NULL DEFAULT 0,
  synonym TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  entity_type TEXT NOT NULL DEFAULT 'configuration',
  version TEXT NOT NULL DEFAULT '',
  file_path TEXT NOT NULL DEFAULT '',
  source_mode TEXT NOT NULL DEFAULT 'report',
  source_location TEXT NOT NULL DEFAULT 'upload',
  source_path TEXT NOT NULL DEFAULT '',
  ingest_profile TEXT NOT NULL DEFAULT '{}',
  dumps_dir TEXT NOT NULL DEFAULT '',
  zip_path TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  bsl_enabled INTEGER NOT NULL DEFAULT 1,
  bsl_load_mode TEXT NOT NULL DEFAULT '',
  bsl_embed_mode TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'uploaded',
  object_count INTEGER NOT NULL DEFAULT 0,
  link_count INTEGER NOT NULL DEFAULT 0,
  indexed_count INTEGER NOT NULL DEFAULT 0,
  index_target INTEGER NOT NULL DEFAULT 0,
  index_started_at REAL NOT NULL DEFAULT 0,
  index_scope TEXT NOT NULL DEFAULT '',
  parse_gen INTEGER NOT NULL DEFAULT 0,
  parse_added INTEGER NOT NULL DEFAULT 0,
  parse_changed INTEGER NOT NULL DEFAULT 0,
  parse_deleted INTEGER NOT NULL DEFAULT 0,
  parse_unchanged INTEGER NOT NULL DEFAULT 0,
  bsl_method_count INTEGER NOT NULL DEFAULT 0,
  bsl_embed_gen INTEGER NOT NULL DEFAULT 0,
  error_message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entity_tags (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (entity_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_tags_tag ON entity_tags(tag_id);

CREATE TABLE IF NOT EXISTS objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  kind_ru TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  synonym TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  belong TEXT NOT NULL DEFAULT 'Own',
  base_object TEXT NOT NULL DEFAULT '',
  props_json TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL DEFAULT '',
  parse_gen INTEGER NOT NULL DEFAULT 0,
  embed_done INTEGER NOT NULL DEFAULT 0,
  bsl_embed_gen INTEGER NOT NULL DEFAULT 0,
  source_rel TEXT NOT NULL DEFAULT '',
  UNIQUE(entity_id, path)
);

CREATE INDEX IF NOT EXISTS idx_objects_entity ON objects(entity_id);
CREATE INDEX IF NOT EXISTS idx_objects_kind ON objects(entity_id, kind);
CREATE INDEX IF NOT EXISTS idx_objects_kind_name ON objects(entity_id, kind, name);
CREATE INDEX IF NOT EXISTS idx_objects_embed ON objects(entity_id, embed_done);
-- Covering index for Help recall: avoids loading HTML comment blobs (~10k rows).
CREATE INDEX IF NOT EXISTS idx_objects_help_cover
  ON objects(entity_id, path, name) WHERE kind = 'Help';

CREATE TABLE IF NOT EXISTS dump_file_state (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (entity_id, rel_path)
);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  from_path TEXT NOT NULL,
  to_ref TEXT NOT NULL,
  link_type TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_links_from ON links(entity_id, from_path);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(entity_id, to_ref);
-- Covering seek by to_ref (find_usages / trace_impact). Without this SQLite often
-- picks idx_links_unique on entity_id alone and scans all links of the config.
CREATE INDEX IF NOT EXISTS idx_links_to_cover ON links(entity_id, to_ref, link_type, from_path);

CREATE TABLE IF NOT EXISTS pending_zvec_deletes (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  doc_id TEXT NOT NULL,
  PRIMARY KEY (entity_id, doc_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  tier TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 1,
  duration_ms REAL NOT NULL DEFAULT 0,
  context TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_app_log_ts ON app_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_app_log_tier_ts ON app_log(tier, ts DESC);
"""

# Колоночные миграции для уже существующих БД (этап 1 мог не иметь
# index_scope; objects/links/pending_zvec_deletes создаются в SCHEMA,
# поэтому таблицы подхватываются автоматически).
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("entities", "index_scope", "TEXT NOT NULL DEFAULT ''"),
    ("entities", "bsl_method_count", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "bsl_embed_gen", "INTEGER NOT NULL DEFAULT 0"),
    ("objects", "content_hash", "TEXT NOT NULL DEFAULT ''"),
    ("objects", "parse_gen", "INTEGER NOT NULL DEFAULT 0"),
    ("objects", "source_rel", "TEXT NOT NULL DEFAULT ''"),
    ("objects", "bsl_embed_gen", "INTEGER NOT NULL DEFAULT 0"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _is_locked_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _retry_locked(fn, *args, **kwargs):
    delay = LOCKED_RETRY_SLEEP_SEC
    last: BaseException | None = None
    for attempt in range(LOCKED_RETRIES + 1):
        try:
            out = fn(*args, **kwargs)
            if attempt > 0:
                try:
                    from app.services import mcp_busy

                    mcp_busy.clear_waiting_flag()
                except Exception:
                    pass
            return out
        except sqlite3.OperationalError as exc:
            last = exc
            if not _is_locked_error(exc) or attempt >= LOCKED_RETRIES:
                raise
            log.warning(
                "sqlite locked, retry %s/%s after %.2fs: %s",
                attempt + 1,
                LOCKED_RETRIES,
                delay,
                exc,
            )
            # Lower bound of lock wait: retry sleeps only (busy_timeout is inside SQLite).
            try:
                from app.services import mcp_busy

                mcp_busy.add_sqlite_wait(delay * 1000.0, retries=1)
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    raise last  # pragma: no cover


class RetryConnection(sqlite3.Connection):
    """Waits busy_timeout, then retries locked execute/commit a couple of times."""

    def execute(self, *args, **kwargs):
        return _retry_locked(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return _retry_locked(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return _retry_locked(super().executescript, *args, **kwargs)

    def commit(self):
        return _retry_locked(super().commit)


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=CONNECT_TIMEOUT_SEC,
        factory=RetryConnection,
    )
    _configure_connection(conn)
    return conn


def _ensure_links_unique(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "links") or _index_exists(conn, "idx_links_unique"):
        return
    conn.execute(
        """
        DELETE FROM links
        WHERE id NOT IN (
          SELECT MIN(id) FROM links
          GROUP BY entity_id, from_path, to_ref, link_type
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_links_unique
        ON links(entity_id, from_path, to_ref, link_type)
        """
    )


def migrate(conn: sqlite3.Connection) -> None:
    """Применяет колоночные миграции и добивается наличия всех таблиц."""
    for table, column, decl in _MIGRATIONS:
        if _table_exists(conn, table) and not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS objects (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          kind_ru TEXT NOT NULL,
          kind TEXT NOT NULL,
          name TEXT NOT NULL,
          synonym TEXT NOT NULL DEFAULT '',
          comment TEXT NOT NULL DEFAULT '',
          belong TEXT NOT NULL DEFAULT 'Own',
          base_object TEXT NOT NULL DEFAULT '',
          props_json TEXT NOT NULL DEFAULT '{}',
          content_hash TEXT NOT NULL DEFAULT '',
          parse_gen INTEGER NOT NULL DEFAULT 0,
          embed_done INTEGER NOT NULL DEFAULT 0,
          bsl_embed_gen INTEGER NOT NULL DEFAULT 0,
          source_rel TEXT NOT NULL DEFAULT '',
          UNIQUE(entity_id, path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dump_file_state (
          entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          rel_path TEXT NOT NULL,
          mtime_ns INTEGER NOT NULL DEFAULT 0,
          size INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (entity_id, rel_path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          from_path TEXT NOT NULL,
          to_ref TEXT NOT NULL,
          link_type TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_zvec_deletes (
          entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          doc_id TEXT NOT NULL,
          PRIMARY KEY (entity_id, doc_id)
        )
        """
    )
    if _table_exists(conn, "objects") and _column_exists(conn, "objects", "parse_gen"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_parse_gen ON objects(entity_id, parse_gen)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_entity ON objects(entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_kind ON objects(entity_id, kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_embed ON objects(entity_id, embed_done)"
    )
    if _table_exists(conn, "objects") and _column_exists(conn, "objects", "bsl_embed_gen"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_objects_bsl_embed
            ON objects(entity_id, kind, bsl_embed_gen)
            """
        )
    if _table_exists(conn, "objects") and _column_exists(conn, "objects", "source_rel"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_source_rel ON objects(entity_id, source_rel)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_name ON objects(entity_id, name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_kind_name ON objects(entity_id, kind, name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_path ON objects(entity_id, path)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_objects_help_cover
        ON objects(entity_id, path, name) WHERE kind = 'Help'
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_links_from ON links(entity_id, from_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_links_to ON links(entity_id, to_ref)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_links_to_cover
        ON links(entity_id, to_ref, link_type, from_path)
        """
    )
    _ensure_links_unique(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL NOT NULL,
          tier TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT '',
          name TEXT NOT NULL DEFAULT '',
          ok INTEGER NOT NULL DEFAULT 1,
          duration_ms REAL NOT NULL DEFAULT 0,
          context TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_log_ts ON app_log(ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_log_tier_ts ON app_log(tier, ts DESC)"
    )
    conn.commit()


_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _db_key(db_path: Path) -> str:
    try:
        return str(db_path.resolve())
    except Exception:
        return str(db_path)


def init_db(db_path: Path) -> None:
    """Apply SCHEMA + migrations once per process/DB path."""
    key = _db_key(db_path)
    with _init_lock:
        if key in _initialized_paths:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(db_path)
        try:
            conn.executescript(SCHEMA)
            migrate(conn)
            _initialized_paths.add(key)
        finally:
            conn.close()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection. Schema/migrate run only via init_db (lazy once if needed)."""
    key = _db_key(db_path)
    if key not in _initialized_paths:
        init_db(db_path)
    return _open(db_path)


@contextmanager
def db_session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
