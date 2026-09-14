"""SQLite persistence for dump profiles and runs."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from app.services.onec_dump import DumpProfileSpec
from app.services.secret_store import decrypt_secret, encrypt_secret


def peek_dump_out_meta(out_dir: str) -> tuple[str, str]:
    """Имя и версия из Configuration.xml в каталоге выгрузки (если уже есть дамп)."""
    raw = (out_dir or "").strip()
    if not raw:
        return "", ""
    try:
        from app.services.dump_parser import read_configuration_meta
        from app.services.dump_zip import resolve_dump_root

        root = resolve_dump_root(Path(raw))
        if not (root / "Configuration.xml").is_file():
            return "", ""
        meta = read_configuration_meta(root)
        return (meta.config_name or "").strip(), (meta.version or "").strip()
    except OSError:
        return "", ""


def _row_to_profile(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["has_password"] = bool(d.get("secret_blob"))
    d.pop("secret_blob", None)
    dump_name, dump_version = peek_dump_out_meta(d.get("out_dir") or "")
    d["dump_config_name"] = dump_name
    d["dump_config_version"] = dump_version
    return d


def list_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, comment, target_type, extension_name, ib_type, ib_address,
               ib_user, secret_blob, platform_path_override, out_dir, dump_mode,
               clear_before_full, post_action, entity_id, timeout_sec,
               last_run_state, last_run_at, created_at, updated_at
        FROM dump_profiles
        ORDER BY name COLLATE NOCASE
        """
    ).fetchall()
    return [_row_to_profile(r) for r in rows]


def get_profile(conn: sqlite3.Connection, profile_id: int) -> dict[str, Any] | None:
    row = get_profile_row(conn, profile_id)
    return _row_to_profile(row) if row else None


def get_profile_row(conn: sqlite3.Connection, profile_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM dump_profiles WHERE id=?", (profile_id,)).fetchone()


def _norm_out_dir(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).casefold()
    except OSError:
        return raw.casefold().replace("\\", "/")


def assert_profile_constraints(
    conn: sqlite3.Connection,
    *,
    out_dir: str,
    entity_id: int | None,
    exclude_profile_id: int | None = None,
) -> None:
    norm = _norm_out_dir(out_dir)
    if norm:
        rows = conn.execute(
            "SELECT id, out_dir FROM dump_profiles WHERE trim(coalesce(out_dir, '')) != ''"
        ).fetchall()
        for row in rows:
            if exclude_profile_id is not None and int(row["id"]) == int(exclude_profile_id):
                continue
            if _norm_out_dir(str(row["out_dir"] or "")) == norm:
                raise ValueError("out_dir_taken")
    if entity_id is not None:
        q = "SELECT id FROM dump_profiles WHERE entity_id=?"
        params: list[Any] = [entity_id]
        if exclude_profile_id is not None:
            q += " AND id != ?"
            params.append(exclude_profile_id)
        if conn.execute(q, params).fetchone():
            raise ValueError("entity_id_taken")


def create_profile(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    out_dir = data.get("out_dir") or ""
    entity_id = data.get("entity_id")
    assert_profile_constraints(conn, out_dir=out_dir, entity_id=entity_id)
    secret_blob = None
    if data.get("password"):
        secret_blob = encrypt_secret(str(data["password"]))
    cur = conn.execute(
        """
        INSERT INTO dump_profiles (
          name, comment, target_type, extension_name, ib_type, ib_address, ib_user,
          secret_blob, platform_path_override, out_dir, dump_mode, clear_before_full,
          post_action, entity_id, timeout_sec
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            data["name"],
            data.get("comment") or "",
            data.get("target_type") or "configuration",
            data.get("extension_name") or "",
            data.get("ib_type") or "file",
            data.get("ib_address") or "",
            data.get("ib_user") or "",
            secret_blob,
            data.get("platform_path_override") or "",
            data.get("out_dir") or "",
            data.get("dump_mode") or "update",
            1 if data.get("clear_before_full") else 0,
            data.get("post_action") or "",
            data.get("entity_id"),
            int(data.get("timeout_sec") or 7200),
        ),
    )
    conn.commit()
    out = get_profile(conn, int(cur.lastrowid))
    assert out is not None
    return out


def update_profile(
    conn: sqlite3.Connection, profile_id: int, data: dict[str, Any]
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM dump_profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        return None
    fields = {
        "name": data.get("name", row["name"]),
        "comment": data.get("comment", row["comment"]),
        "target_type": data.get("target_type", row["target_type"]),
        "extension_name": data.get("extension_name", row["extension_name"]),
        "ib_type": data.get("ib_type", row["ib_type"]),
        "ib_address": data.get("ib_address", row["ib_address"]),
        "ib_user": data.get("ib_user", row["ib_user"]),
        "platform_path_override": data.get(
            "platform_path_override", row["platform_path_override"]
        ),
        "out_dir": data.get("out_dir", row["out_dir"]),
        "dump_mode": data.get("dump_mode", row["dump_mode"]),
        "clear_before_full": data.get("clear_before_full", bool(row["clear_before_full"])),
        "post_action": data.get("post_action", row["post_action"]),
        "entity_id": data.get("entity_id", row["entity_id"]),
        "timeout_sec": data.get("timeout_sec", row["timeout_sec"]),
    }
    secret_blob = row["secret_blob"]
    # PATCH password contract: key absent → keep; non-empty → replace; "" → delete.
    if "password" in data:
        pwd = data["password"]
        if pwd is None or pwd == "":
            secret_blob = None
        else:
            secret_blob = encrypt_secret(str(pwd))
    assert_profile_constraints(
        conn,
        out_dir=str(fields["out_dir"] or ""),
        entity_id=fields["entity_id"],
        exclude_profile_id=profile_id,
    )
    conn.execute(
        """
        UPDATE dump_profiles SET
          name=?, comment=?, target_type=?, extension_name=?, ib_type=?, ib_address=?,
          ib_user=?, secret_blob=?, platform_path_override=?, out_dir=?, dump_mode=?,
          clear_before_full=?, post_action=?, entity_id=?, timeout_sec=?,
          updated_at=datetime('now')
        WHERE id=?
        """,
        (
            fields["name"],
            fields["comment"],
            fields["target_type"],
            fields["extension_name"],
            fields["ib_type"],
            fields["ib_address"],
            fields["ib_user"],
            secret_blob,
            fields["platform_path_override"],
            fields["out_dir"],
            fields["dump_mode"],
            1 if fields["clear_before_full"] else 0,
            fields["post_action"],
            fields["entity_id"],
            int(fields["timeout_sec"]),
            profile_id,
        ),
    )
    conn.commit()
    return get_profile(conn, profile_id)


def suggest_copy_profile_name(
    conn: sqlite3.Connection, source_name: str, requested: str | None = None
) -> str:
    """Unique name for a profile copy (``Source_2``, ``Source_3``, …)."""
    if requested and str(requested).strip():
        cand = str(requested).strip()
        row = conn.execute("SELECT 1 FROM dump_profiles WHERE name=?", (cand,)).fetchone()
        if row:
            raise ValueError("name_taken")
        return cand
    base = (source_name or "").strip() or "profile"
    for i in range(2, 1000):
        cand = f"{base}_{i}"
        if not conn.execute("SELECT 1 FROM dump_profiles WHERE name=?", (cand,)).fetchone():
            return cand
    raise ValueError("no_unique_name")


def copy_profile(
    conn: sqlite3.Connection, profile_id: int, *, name: str | None = None
) -> dict[str, Any] | None:
    """Duplicate dump profile settings, including encrypted password blob.

    Extension name, out_dir and post-import fields (post_action, entity_id) are reset.
    """
    row = get_profile_row(conn, profile_id)
    if not row:
        return None
    new_name = suggest_copy_profile_name(conn, str(row["name"]), name)
    cur = conn.execute(
        """
        INSERT INTO dump_profiles (
          name, comment, target_type, extension_name, ib_type, ib_address, ib_user,
          secret_blob, platform_path_override, out_dir, dump_mode, clear_before_full,
          post_action, entity_id, timeout_sec
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            new_name,
            row["comment"],
            row["target_type"],
            "",
            row["ib_type"],
            row["ib_address"],
            row["ib_user"],
            row["secret_blob"],
            row["platform_path_override"],
            "",
            row["dump_mode"],
            row["clear_before_full"],
            "",
            None,
            int(row["timeout_sec"] or 7200),
        ),
    )
    conn.commit()
    out = get_profile(conn, int(cur.lastrowid))
    assert out is not None
    return out


def delete_profile(conn: sqlite3.Connection, profile_id: int) -> bool:
    cur = conn.execute("DELETE FROM dump_profiles WHERE id=?", (profile_id,))
    conn.commit()
    return cur.rowcount > 0


def profile_has_active_run(conn: sqlite3.Connection, profile_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM dump_runs
        WHERE profile_id=? AND state IN ('queued', 'running')
        LIMIT 1
        """,
        (profile_id,),
    ).fetchone()
    return row is not None


def _next_queue_order(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(queue_order), 0) AS m FROM dump_runs
        WHERE state IN ('queued', 'running')
        """
    ).fetchone()
    return float(row["m"] or 0) + 1.0


def create_run(
    conn: sqlite3.Connection,
    profile_id: int,
    *,
    submit_parse: bool = True,
) -> int:
    if profile_has_active_run(conn, profile_id):
        raise ValueError("active_run_exists")
    now = time.time()
    qo = _next_queue_order(conn)
    cur = conn.execute(
        """
        INSERT INTO dump_runs (profile_id, state, last_activity_at, queue_order, submit_parse)
        VALUES (?, 'queued', ?, ?, ?)
        """,
        (profile_id, now, qo, 1 if submit_parse else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_queue(conn: sqlite3.Connection) -> dict[str, Any]:
    running_row = conn.execute(
        """
        SELECT r.*, p.name AS profile_name
        FROM dump_runs r
        JOIN dump_profiles p ON p.id = r.profile_id
        WHERE r.state = 'running'
        ORDER BY r.id ASC
        LIMIT 1
        """
    ).fetchone()
    pending_rows = conn.execute(
        """
        SELECT r.*, p.name AS profile_name
        FROM dump_runs r
        JOIN dump_profiles p ON p.id = r.profile_id
        WHERE r.state = 'queued'
        ORDER BY r.queue_order ASC, r.id ASC
        """
    ).fetchall()
    running: dict[str, Any] | None = None
    if running_row:
        running = dict(running_row)
        running["queue_position"] = 0
    base = 1 if running else 0
    pending: list[dict[str, Any]] = []
    for i, row in enumerate(pending_rows):
        item = dict(row)
        item["queue_position"] = i + 1 + base
        pending.append(item)
    return {"running": running, "pending": pending}


def reorder_queued_runs(conn: sqlite3.Connection, run_ids: list[int]) -> None:
    queued = conn.execute(
        """
        SELECT id FROM dump_runs WHERE state='queued'
        ORDER BY queue_order ASC, id ASC
        """
    ).fetchall()
    expected = [int(r["id"]) for r in queued]
    ids = [int(x) for x in run_ids]
    if len(ids) != len(expected):
        raise ValueError("run_ids length must match queued runs")
    if set(ids) != set(expected):
        raise ValueError("run_ids must list every queued run exactly once")
    for pos, run_id in enumerate(ids, start=1):
        conn.execute(
            "UPDATE dump_runs SET queue_order=? WHERE id=? AND state='queued'",
            (float(pos), run_id),
        )
    conn.commit()


def list_runs(
    conn: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    entity_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    if entity_id is not None:
        rows = conn.execute(
            """
            SELECT r.* FROM dump_runs r
            INNER JOIN dump_profiles p ON p.id = r.profile_id
            WHERE p.entity_id=?
            ORDER BY r.id DESC LIMIT ?
            """,
            (entity_id, limit),
        ).fetchall()
    elif profile_id is not None:
        rows = conn.execute(
            """
            SELECT * FROM dump_runs WHERE profile_id=?
            ORDER BY id DESC LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM dump_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM dump_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def profile_password(profile_row: sqlite3.Row | dict[str, Any]) -> str:
    blob = profile_row["secret_blob"] if isinstance(profile_row, sqlite3.Row) else profile_row.get("secret_blob")
    if not blob:
        return ""
    result = decrypt_secret(blob)
    return result.value if result.ok else ""


def profile_to_spec(
    profile_row: sqlite3.Row | dict[str, Any],
    *,
    platform_path: str,
    password: str | None = None,
) -> DumpProfileSpec:
    pwd = profile_password(profile_row) if password is None else password
    row = dict(profile_row) if isinstance(profile_row, sqlite3.Row) else profile_row
    override = (row.get("platform_path_override") or "").strip()
    return DumpProfileSpec(
        name=row.get("name") or "",
        target_type=row.get("target_type") or "configuration",
        extension_name=row.get("extension_name") or "",
        ib_type=row.get("ib_type") or "file",
        ib_address=row.get("ib_address") or "",
        ib_user=row.get("ib_user") or "",
        password=pwd,
        platform_path=override or platform_path,
        out_dir=row.get("out_dir") or "",
        dump_mode=row.get("dump_mode") or "update",
        clear_before_full=bool(row.get("clear_before_full")),
        timeout_sec=int(row.get("timeout_sec") or 7200),
    )


def entity_context(conn: sqlite3.Connection, entity_id: int | None) -> tuple[str, str]:
    if not entity_id:
        return "", ""
    row = conn.execute(
        "SELECT name, entity_type FROM entities WHERE id=?",
        (entity_id,),
    ).fetchone()
    if not row:
        return "", ""
    return str(row["name"] or ""), str(row["entity_type"] or "")


def dump_log_dir(data_dir: Path) -> Path:
    path = data_dir / "dump-logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
