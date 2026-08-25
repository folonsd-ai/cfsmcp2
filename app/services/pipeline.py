from __future__ import annotations

import gc
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import zvec

from app.core.config import settings
from app.core.database import connect
from app.repositories import dump_files as dump_file_repo
from app.repositories import entities as ent_repo
from app.repositories import objects as obj_repo
from app.services.embeddings import EmbeddingClient, LmStudioUnavailable
from app.services.parser import (
    TextDecodeStats,
    decode_bytes,
    extract_links,
    iter_report_nodes,
    meta_from_first_node,
    node_fields,
)
from app.services.dump_file_index import (
    REPORT_FILE_REL,
    DumpFileTracker,
    collect_sidecars,
    file_stamp,
    tracked_files_unchanged,
)
from app.services import runtime_settings
from app.services.path_nfc import nfc_rel
from app.services.usage_stats import usage_stats
from app.services.zvec_store import zvec_store

log = logging.getLogger("cfsmcp2.pipeline")

_TEMP_UPLOAD_RE = re.compile(r"^\.upload_[0-9a-f]+\.part$", re.I)
_TEMP_MODULES_FULL_RE = re.compile(r"^\.modules_full_[0-9a-f]+\.zip$", re.I)
_TEMP_MODULES_ENTITY_RE = re.compile(r"^\.modules_(\d+)_[0-9a-f]+\.zip$", re.I)
_ENTITY_REPORT_RE = re.compile(r"^e(\d+)\.txt$", re.I)
_PENDING_BSL_RE = re.compile(r"^e(\d+)\.bsl\.pending\.zip$", re.I)
_DUMP_AUDIT_RE = re.compile(r"^e(\d+)\.dump\.audit\.jsonl$", re.I)

# Ignore in-flight uploads younger than this (seconds) on startup sweep.
_METADATA_TEMP_GRACE_SEC = 3600.0


class _PhaseTimer:
    """Accumulate wall-time per named phase; log a one-line summary."""

    __slots__ = ("_phases", "_current", "_t0", "_t_phase")

    def __init__(self) -> None:
        self._phases: list[tuple[str, float]] = []
        self._current: str | None = None
        self._t0 = time.perf_counter()
        self._t_phase = self._t0

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        if self._current is not None:
            self._phases.append((self._current, now - self._t_phase))
        self._current = name
        self._t_phase = now

    def add(self, name: str, seconds: float) -> None:
        if seconds <= 0:
            return
        self._phases.append((name, float(seconds)))

    def finish(self) -> None:
        now = time.perf_counter()
        if self._current is not None:
            self._phases.append((self._current, now - self._t_phase))
            self._current = None
        self._t_phase = now

    @property
    def total_sec(self) -> float:
        return time.perf_counter() - self._t0

    def summary(self) -> str:
        self.finish()
        parts = [f"{name}={sec:.1f}s" for name, sec in self._phases if sec >= 0.05]
        if not parts:
            parts = [f"{name}={sec:.2f}s" for name, sec in self._phases]
        parts.append(f"total={self.total_sec:.1f}s")
        return " ".join(parts)

    def snapshot(self) -> dict[str, Any]:
        """timing_ms + bottleneck for persist log (after summary/finish)."""
        self.finish()
        timing_ms = {name: int(round(sec * 1000)) for name, sec in self._phases if sec > 0}
        total_ms = int(round(self.total_sec * 1000))
        timing_ms["total"] = total_ms
        bottleneck = ""
        if self._phases:
            bottleneck = max(self._phases, key=lambda kv: kv[1])[0]
        return {
            "timing_ms": timing_ms,
            "bottleneck": bottleneck,
            "duration_ms": float(total_ms),
        }

    def log(self, kind: str, entity_id: int, *, summary: str | None = None, **extra: Any) -> None:
        text = summary if summary is not None else self.summary()
        extras = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None and v != "")
        msg = f"{kind} timing entity={entity_id} {text}"
        if extras:
            msg = f"{msg} {extras}"
        log.info("%s", msg)


_MIN_NOOP_INFO_MS = 2000.0


def _persist_index_job(
    *,
    name: str,
    entity: dict,
    timer: _PhaseTimer,
    scale: dict[str, Any],
    note: str = "",
    skip_if_short: bool = False,
) -> None:
    """One persist row per completed parse/reindex (tier=info). Not MCP slow."""
    snap = timer.snapshot()
    duration_ms = float(snap["duration_ms"])
    if skip_if_short and duration_ms < _MIN_NOOP_INFO_MS:
        return
    from app.services import app_log as app_log_svc

    detail = app_log_svc.format_index_job_detail(
        job=name,
        duration_ms=duration_ms,
        timing_ms=snap.get("timing_ms") if isinstance(snap.get("timing_ms"), dict) else None,
        bottleneck=str(snap.get("bottleneck") or ""),
        scale=scale,
        note=note,
    )
    app_log_svc.append_info(
        kind="index",
        name=name,
        duration_ms=duration_ms,
        context=str(entity.get("name") or ""),
        detail=detail,
    )


def collection_path(entity_id: int, model: str) -> Path:
    """Filesystem-safe path by entity id (avoid Cyrillic / special chars on Windows)."""
    safe_model = model.replace("/", "_").replace(":", "_").replace("@", "_")
    return settings.zvec_dir / f"e{int(entity_id)}__{safe_model}"


def entity_report_path(entity_id: int) -> Path:
    """Canonical on-disk report path — unique per entity."""
    return settings.metadata_dir / f"e{int(entity_id)}.txt"


def pending_bsl_zip_path(entity_id: int) -> Path:
    """Zip of modules to ingest after parse+index (full import / deferred upload)."""
    return settings.metadata_dir / f"e{int(entity_id)}.bsl.pending.zip"


def entity_dump_dir(entity_id: int) -> Path:
    """Server-owned unpacked dump directory (upload mode)."""
    return settings.dumps_dir / f"e{int(entity_id)}"


def entity_zip_path(entity_id: int) -> Path:
    """Server-owned dump zip (upload mode)."""
    return settings.dumps_dir / f"e{int(entity_id)}.zip"


def entity_dump_audit_path(entity_id: int) -> Path:
    """NDJSON audit of dump files that produced no objects (этап 3)."""
    return settings.metadata_dir / f"e{int(entity_id)}.dump.audit.jsonl"


def _dump_artifacts_for_entity(entity: dict | None) -> list[Path]:
    """Server-owned dump artifacts (dumps/e{id}/ + zip), только для upload-режима.

    В path-режиме ``dumps_dir`` указывает на внешний каталог пользователя —
    серверные артефакты не создаются и не удаляются.
    """
    if not entity:
        return []
    if (entity.get("source_location") or "").strip() != "upload":
        return []
    eid = int(entity["id"])
    return [entity_dump_dir(eid), entity_zip_path(eid)]


def delete_entity_upload_files(
    entity_id: int,
    *,
    extra_paths: list[Path] | None = None,
) -> int:
    """Remove report / pending BSL / module temps / dump artifacts for one entity.

    Safety (этап 2.5/3): удаляет только файлы, которыми владеет cfsmcp2:
    ``metadata_dir/e{id}.txt`` (в path-режиме такого файла нет), pending BSL zip,
    module-temps внутри ``metadata_dir``, dump audit ``e{id}.dump.audit.jsonl`` и,
    для upload-режима dump, каталог ``dumps/e{id}/`` + zip ``dumps/e{id}.zip``.
    Внешние файлы/каталоги источника (source_location=path) в список кандидатов
    не попадают; вызывающий должен передать их явно через ``extra_paths``
    (для path-режима вызывается с ``extra_paths=[]``).
    """
    eid = int(entity_id)
    root = settings.metadata_dir
    candidates: list[Path] = [
        entity_report_path(eid),
        pending_bsl_zip_path(eid),
        entity_dump_audit_path(eid),
    ]
    if root.is_dir():
        for p in root.iterdir():
            if not p.is_file():
                continue
            m = _TEMP_MODULES_ENTITY_RE.match(p.name)
            if m and int(m.group(1)) == eid:
                candidates.append(p)
    try:
        conn = connect(settings.db_path)
        try:
            entity = ent_repo.get_entity(conn, eid)
            candidates.extend(_dump_artifacts_for_entity(entity))
        finally:
            conn.close()
    except Exception:
        log.debug("failed loading entity for dump artifacts %s", eid, exc_info=True)
    if extra_paths:
        for p in extra_paths:
            if p is not None:
                candidates.append(Path(p))

    removed = 0
    seen: set[Path] = set()
    for p in candidates:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                if p.exists():
                    continue
                removed += 1
                log.info("removed entity upload dir entity=%s path=%s", eid, p.name)
                continue
            if not p.exists() or not p.is_file():
                continue
            p.unlink(missing_ok=True)
            removed += 1
            log.info("removed entity upload file entity=%s path=%s", eid, p.name)
        except OSError:
            log.debug("failed removing entity upload file %s", p, exc_info=True)
    return removed


def _file_age_sec(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def _safe_unlink_metadata(path: Path, *, reason: str) -> bool:
    try:
        if not path.is_file():
            return False
        path.unlink(missing_ok=True)
        log.info("metadata cleanup: removed %s (%s)", path.name, reason)
        return True
    except OSError:
        log.debug("metadata cleanup: failed %s", path, exc_info=True)
        return False


def backfill_entity_types() -> dict[str, int]:
    """Detect CF/CFE from on-disk reports and fix ``entities.entity_type``.

    Runs on startup so older rows get corrected.
    """
    from app.services.parser import peek_report_meta

    stats = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}
    conn = connect(settings.db_path)
    try:
        rows = ent_repo.list_entities(conn)
        for row in rows:
            stats["checked"] += 1
            eid = int(row["id"])
            path = Path(row["file_path"]) if row.get("file_path") else entity_report_path(eid)
            if not path.is_file():
                stats["skipped"] += 1
                continue
            try:
                meta = peek_report_meta(path, path.stem)
                detected = meta.entity_type or "configuration"
                current = (row.get("entity_type") or "").strip() or "configuration"
                if detected == current:
                    stats["skipped"] += 1
                    continue
                ent_repo.set_entity_type(conn, eid, detected)
                stats["updated"] += 1
            except Exception:
                stats["errors"] += 1
                log.debug("entity_type backfill failed for #%s", eid, exc_info=True)
        conn.commit()
    finally:
        conn.close()
    return stats


def cleanup_metadata_orphans(*, grace_sec: float = _METADATA_TEMP_GRACE_SEC) -> dict[str, int]:
    """Remove leftover upload temps and files for deleted / unknown entities.

    Keeps ``e{id}.*`` for live ids and any path still referenced by ``entities.file_path``.
    Temps younger than ``grace_sec`` are left alone (in-flight uploads).
    """
    stats = {
        "scanned": 0,
        "removed_temp": 0,
        "removed_orphan_report": 0,
        "removed_orphan_pending": 0,
        "removed_orphan_dump": 0,
        "removed_orphan_other": 0,
        "kept": 0,
    }
    root = Path(settings.metadata_dir)
    if not root.is_dir():
        return stats

    conn = connect(settings.db_path)
    try:
        rows = ent_repo.list_entities(conn)
        live_ids = {int(r["id"]) for r in rows}
        keep: set[Path] = set()
        for r in rows:
            eid = int(r["id"])
            for p in (entity_report_path(eid), pending_bsl_zip_path(eid)):
                try:
                    keep.add(p.resolve())
                except OSError:
                    keep.add(p)
            fp = (r.get("file_path") or "").strip()
            if fp:
                try:
                    keep.add(Path(fp).resolve())
                except OSError:
                    keep.add(Path(fp))
    finally:
        conn.close()

    grace = max(0.0, float(grace_sec))
    for path in root.iterdir():
        if not path.is_file():
            continue
        stats["scanned"] += 1
        name = path.name
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path

        if resolved in keep:
            stats["kept"] += 1
            continue

        age = _file_age_sec(path)

        if _TEMP_UPLOAD_RE.match(name) or _TEMP_MODULES_FULL_RE.match(name):
            if age >= grace and _safe_unlink_metadata(path, reason="stale temp"):
                stats["removed_temp"] += 1
            else:
                stats["kept"] += 1
            continue

        m_mod = _TEMP_MODULES_ENTITY_RE.match(name)
        if m_mod:
            eid = int(m_mod.group(1))
            if eid not in live_ids:
                if _safe_unlink_metadata(path, reason=f"modules temp for deleted entity {eid}"):
                    stats["removed_temp"] += 1
            elif age >= grace and _safe_unlink_metadata(path, reason="stale modules temp"):
                stats["removed_temp"] += 1
            else:
                stats["kept"] += 1
            continue

        m_rep = _ENTITY_REPORT_RE.match(name)
        if m_rep:
            eid = int(m_rep.group(1))
            if eid not in live_ids and _safe_unlink_metadata(
                path, reason=f"orphan report e{eid}.txt"
            ):
                stats["removed_orphan_report"] += 1
            else:
                stats["kept"] += 1
            continue

        m_pending = _PENDING_BSL_RE.match(name)
        if m_pending:
            eid = int(m_pending.group(1))
            if eid not in live_ids and _safe_unlink_metadata(
                path, reason=f"orphan pending BSL e{eid}"
            ):
                stats["removed_orphan_pending"] += 1
            else:
                # Live entity: keep for recover / in-flight ingest
                stats["kept"] += 1
            continue

        m_dump_audit = _DUMP_AUDIT_RE.match(name)
        if m_dump_audit:
            eid = int(m_dump_audit.group(1))
            if eid not in live_ids and _safe_unlink_metadata(
                path, reason=f"orphan dump audit e{eid}"
            ):
                stats["removed_orphan_dump"] += 1
            else:
                stats["kept"] += 1
            continue

        # Legacy / unknown files not referenced by any entity
        if age >= grace and _safe_unlink_metadata(path, reason="unreferenced metadata file"):
            stats["removed_orphan_other"] += 1
        else:
            stats["kept"] += 1

    # Server-owned dump artifacts for deleted entities (dumps/e{id}/ and e{id}.zip)
    dumps_root = Path(settings.dumps_dir)
    if dumps_root.is_dir():
        for d in dumps_root.iterdir():
            if d.name in {"."}:
                continue
            if d.is_dir() and d.name.isdigit():
                continue  # не наш формат (e{id})
            if not (d.name.startswith("e") and d.name[1:].split(".", 1)[0].isdigit()):
                continue
            eid = int(d.name[1:].split(".", 1)[0])
            if eid in live_ids:
                stats["kept"] += 1
                continue
            try:
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink(missing_ok=True)
                stats["removed_orphan_dump"] += 1
                log.info("metadata cleanup: removed dump artifact %s", d.name)
            except OSError:
                log.debug("metadata cleanup: failed removing dump artifact %s", d, exc_info=True)

    return stats


def _close_collection(collection) -> None:
    if collection is None:
        return
    try:
        collection.flush()
    except Exception:
        log.debug("flush failed", exc_info=True)
    try:
        if hasattr(collection, "close"):
            collection.close()
    except Exception:
        log.debug("close failed", exc_info=True)
    try:
        del collection
    except Exception:
        pass
    gc.collect()


def _destroy_path(path: Path) -> None:
    """Close any open handle and remove a zvec collection directory."""
    zvec_store.release(path)
    if not path.exists():
        return
    try:
        coll = zvec.open(str(path))
        try:
            coll.destroy()
        finally:
            del coll
            gc.collect()
    except Exception:
        log.debug("destroy via open failed for %s", path, exc_info=True)
    for attempt in range(8):
        if not path.exists():
            return
        shutil.rmtree(path, ignore_errors=True)
        gc.collect()
        if not path.exists():
            return
        time.sleep(0.1 * (attempt + 1))
    if path.exists():
        log.warning("could not fully remove zvec path %s", path)


def _is_zvec_residue_name(name: str) -> bool:
    n = name.lower()
    return (
        n.endswith(".tmp")
        or n.endswith(".tmp.partial")
        or n.endswith(".__tmp__")
        or ".tmp." in n
        or n.endswith(".proxima.bak")
        or n.endswith(".ipc.bak")
    )


def cleanup_zvec_crash_residue(coll_path: Path | None = None) -> int:
    """Remove crash/temp leftovers next to or inside zvec collections.

    After abrupt stop zvec may leave ``N.tmp/`` dirs and stale segment files;
    clearing them before reopen avoids noisy WARN and copy→rebuild fallbacks.
    If ``coll_path`` is None, scan all collections under ``ZVEC_DIR``.
    """
    removed = 0
    roots: list[Path] = []
    if coll_path is not None:
        roots.append(Path(coll_path))
    else:
        zdir = Path(settings.zvec_dir)
        if zdir.is_dir():
            roots.extend(p for p in zdir.iterdir() if p.is_dir())

    for root in roots:
        zvec_store.release(root)
        for sibling in (
            Path(str(root) + ".__tmp__"),
            Path(str(root) + ".tmp"),
            Path(str(root) + ".bak"),
        ):
            if not sibling.exists():
                continue
            try:
                if sibling.is_dir():
                    _destroy_path(sibling)
                else:
                    sibling.unlink(missing_ok=True)
                removed += 1
                log.info("removed zvec residue %s", sibling)
            except Exception:
                log.debug("failed removing residue %s", sibling, exc_info=True)

        if not root.is_dir():
            continue
        # Deepest paths first so nested temps go before parents
        candidates = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
        for p in candidates:
            if not _is_zvec_residue_name(p.name):
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink(missing_ok=True)
                removed += 1
                log.info("removed zvec residue %s", p)
            except Exception:
                log.debug("failed removing residue %s", p, exc_info=True)
    return removed


def _reindex_full(
    conn,
    client: EmbeddingClient,
    entity_id: int,
    model: str,
    coll_path: Path,
    dims: int,
) -> tuple[int, float, float, float, float, float, float]:
    """Build a fresh zvec collection in place (no temp→rename: fails on Win bind mounts).

    Returns ``(indexed, setup_sec, embed_sec, upsert_sec, stale_delete_sec,
    wave_flush_sec, zvec_flush_sec)``.
    """
    t_setup = time.perf_counter()
    # Leftovers from older temp+replace attempts
    _destroy_path(Path(str(coll_path) + ".__tmp__"))
    _destroy_path(coll_path)
    if coll_path.exists():
        raise RuntimeError(
            f"Cannot create zvec collection: path still exists ({coll_path}); "
            "release other handles and retry incremental reindex"
        )
    obj_repo.reset_embed_flags(conn, entity_id)
    conn.commit()

    collection = None
    setup_sec = 0.0
    embed_sec = 0.0
    upsert_sec = 0.0
    stale_delete_sec = 0.0
    wave_flush_sec = 0.0
    zvec_flush_sec = 0.0
    indexed = 0
    try:
        collection = zvec.create_and_open(str(coll_path), build_schema(dims))
        setup_sec = time.perf_counter() - t_setup
        indexed, embed_sec, upsert_sec, stale_delete_sec, wave_flush_sec = _embed_pending_into(
            conn, client, entity_id, model, collection, use_upsert=False
        )
        t_flush = time.perf_counter()
        try:
            collection.flush()
        except Exception:
            log.debug("flush after full reindex failed", exc_info=True)
        try:
            if hasattr(collection, "optimize"):
                collection.optimize()
        except Exception:
            log.debug("optimize after full reindex failed", exc_info=True)
        zvec_flush_sec = time.perf_counter() - t_flush
    finally:
        _close_collection(collection)
    obj_repo.clear_pending_zvec_deletes(conn, entity_id)
    conn.commit()
    return (
        indexed,
        setup_sec,
        embed_sec,
        upsert_sec,
        stale_delete_sec,
        wave_flush_sec,
        zvec_flush_sec,
    )


def build_schema(dims: int) -> zvec.CollectionSchema:
    fts = zvec.FtsIndexParam(
        tokenizer_name="standard",
        filters=["lowercase", "stemmer"],
        extra_params='{"stemmer_lang":"russian"}',
    )
    return zvec.CollectionSchema(
        name="meta",
        fields=[
            zvec.FieldSchema(name="path", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(
                name="kind",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                name="belong",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(name="name", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name="synonym", data_type=zvec.DataType.STRING),
            zvec.FieldSchema(
                name="text",
                data_type=zvec.DataType.STRING,
                index_param=fts,
            ),
        ],
        vectors=[
            zvec.VectorSchema(
                name="embedding",
                data_type=zvec.DataType.VECTOR_FP32,
                dimension=dims,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
        ],
    )


def _apply_report_meta(conn, entity_id: int, entity: dict, meta) -> tuple[int, dict, Path]:
    """Update synonym/version/comment; rename from report Имя when not name_locked.

    Comment defaults to report Имя + Версия; custom comments are kept.
    Name conflicts are resolved at upload (merge confirm or Name override) — not here.
    """
    path = Path(entity["file_path"])
    locked = bool(entity.get("name_locked"))
    target_name = (meta.config_name or "").strip() or entity["name"]
    auto_comment = ent_repo.comment_from_name_version(target_name, meta.version or "")
    old_auto = ent_repo.comment_from_name_version(
        entity.get("name") or "", entity.get("version") or ""
    )
    prev_comment = (entity.get("comment") or "").strip()
    new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment

    if locked:
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (meta.config_synonym, meta.version, new_comment, meta.entity_type or "configuration", entity_id),
        )
        row = ent_repo.get_entity(conn, entity_id) or entity
        return entity_id, row, path

    if target_name and target_name != entity["name"]:
        other = ent_repo.get_entity_by_name(conn, target_name)
        if other is not None and int(other["id"]) != int(entity_id):
            raise ValueError(
                f"Report name '{target_name}' already used by entity #{other['id']}. "
                "Re-upload with merge confirmation or a different Name override."
            )
        conn.execute(
            """
            UPDATE entities SET name=?, synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (
                target_name,
                meta.config_synonym,
                meta.version,
                new_comment,
                meta.entity_type or "configuration",
                entity_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (meta.config_synonym, meta.version, new_comment, meta.entity_type or "configuration", entity_id),
        )
    row = ent_repo.get_entity(conn, entity_id) or entity
    return entity_id, row, path


def _apply_dump_meta(conn, entity_id: int, entity: dict, meta) -> tuple[int, dict, Path]:
    """Update synonym/version/entity_type from Configuration.xml dump meta.

    Name берётся из Configuration.xml при !name_locked (конфликт имён — как у
    report: merge подтверждение или Name override). ``path`` здесь — dumps_dir.
    """
    path = Path(entity.get("dumps_dir") or entity.get("file_path") or "")
    locked = bool(entity.get("name_locked"))
    target_name = (meta.config_name or "").strip() or entity["name"]
    auto_comment = ent_repo.comment_from_name_version(target_name, meta.version or "")
    old_auto = ent_repo.comment_from_name_version(
        entity.get("name") or "", entity.get("version") or ""
    )
    prev_comment = (entity.get("comment") or "").strip()
    new_comment = auto_comment if (not prev_comment or prev_comment == old_auto) else prev_comment

    if locked:
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (meta.config_synonym, meta.version, new_comment, meta.entity_type or "configuration", entity_id),
        )
        row = ent_repo.get_entity(conn, entity_id) or entity
        return entity_id, row, path

    if target_name and target_name != entity["name"]:
        other = ent_repo.get_entity_by_name(conn, target_name)
        if other is not None and int(other["id"]) != int(entity_id):
            raise ValueError(
                f"Configuration name '{target_name}' already used by entity #{other['id']}. "
                "Re-upload with merge confirmation or a different Name override."
            )
        conn.execute(
            """
            UPDATE entities SET name=?, synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (
                target_name,
                meta.config_synonym,
                meta.version,
                new_comment,
                meta.entity_type or "configuration",
                entity_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE entities SET synonym=?, version=?, comment=?, entity_type=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (meta.config_synonym, meta.version, new_comment, meta.entity_type or "configuration", entity_id),
        )
    row = ent_repo.get_entity(conn, entity_id) or entity
    return entity_id, row, path


def _flush_merge_batches(
    conn,
    entity_id: int,
    *,
    new_rows: list[dict],
    changed_rows: list[dict],
    touch_ids: list,
    parse_gen: int,
) -> None:
    if new_rows:
        obj_repo.insert_new_objects(conn, entity_id, new_rows)
        new_rows.clear()
    if changed_rows:
        obj_repo.update_changed_objects(conn, changed_rows)
        changed_rows.clear()
    if touch_ids:
        obj_repo.touch_unchanged_objects(conn, touch_ids, parse_gen)
        touch_ids.clear()


def _dedupe_fields_by_path(fields_batch: list[dict]) -> list[dict]:
    """Keep last occurrence per path (reports may emit the same path more than once)."""
    by_path: dict[str, dict] = {}
    for fields in fields_batch:
        by_path[fields["path"]] = fields
    return list(by_path.values())


def _classify_and_queue(
    fields_batch: list[dict],
    existing: dict[str, tuple[int, str]],
    *,
    new_rows: list[dict],
    changed_rows: list[dict],
    touch_ids: list,
) -> tuple[int, int, int]:
    """Split a fields batch into new/changed/unchanged queues. Returns added, changed, unchanged."""
    added = changed = unchanged = 0
    # Paths already queued as new in this classify pass (within-batch / cross-lookup gap)
    queued_new: dict[str, int] = {}
    for fields in fields_batch:
        path = fields["path"]
        prev = existing.get(path)
        if prev is None and path not in queued_new:
            new_rows.append(fields)
            queued_new[path] = len(new_rows) - 1
            added += 1
        elif prev is None:
            # Duplicate path in same batch — replace earlier "new" row
            new_rows[queued_new[path]] = fields
        else:
            obj_id, old_hash = prev
            if old_hash == fields["content_hash"] and old_hash:
                touch_ids.append((obj_id, fields.get("source_rel") or ""))
                unchanged += 1
            else:
                fields["id"] = obj_id
                changed_rows.append(fields)
                changed += 1
    return added, changed, unchanged


OBJECT_FLUSH = 500
LINK_FLUSH = 1000

# Доли фаз dump-парсинга (верхние границы, финал = 99).
_PARSE_META_HI = 55
_PARSE_ASSETS_HI = 70
_PARSE_HELP_HI = 78
_PARSE_BSL_HI = 97
_PARSE_FINAL_PCT = 99


def _parse_phase_pct(lo: int, hi: int, done: int, total: int) -> int:
    if total <= 0:
        return hi
    return min(hi, max(lo, lo + int((hi - lo) * min(done, total) / total)))


def _set_parse_progress(
    conn,
    entity_id: int,
    pct: int,
    *,
    object_count: int | None = None,
    link_count: int | None = None,
    parse_added: int | None = None,
    parse_changed: int | None = None,
    parse_unchanged: int | None = None,
) -> None:
    """Пишет прогресс парсинга в indexed_count/index_target (% из 100)."""
    ent_repo.set_status(
        conn,
        entity_id,
        "parsing",
        indexed_count=max(0, min(99, int(pct))),
        index_target=100,
        object_count=object_count,
        link_count=link_count,
        parse_added=parse_added,
        parse_changed=parse_changed,
        parse_unchanged=parse_unchanged,
    )


def _count_dump_meta_files(dumps_dir: Path) -> int:
    from app.services.kinds import CONTAINER_EN_TO_RU

    n = 0
    for container_en in CONTAINER_EN_TO_RU:
        cdir = dumps_dir / container_en
        if not cdir.is_dir():
            continue
        try:
            n += sum(1 for p in cdir.glob("*.xml") if p.is_file())
        except OSError:
            pass
    return max(n, 1)


def _count_dump_asset_files(dumps_dir: Path, profile: dict) -> int:
    from pathlib import PurePosixPath

    from app.services.dump_assets_parser import (
        _profile_flag,
        is_managed_form_xml_rel,
        is_template_ext_xml_rel,
    )

    n = 0
    if _profile_flag(profile, "managed_forms"):
        try:
            for p in dumps_dir.rglob("Form.xml"):
                if not p.is_file():
                    continue
                rel = PurePosixPath(nfc_rel(p.relative_to(dumps_dir).as_posix()))
                if is_managed_form_xml_rel(rel):
                    n += 1
        except OSError:
            pass
    if _profile_flag(profile, "dcs") or _profile_flag(profile, "spreadsheet_templates"):
        try:
            for p in dumps_dir.rglob("Template.xml"):
                if not p.is_file():
                    continue
                rel = PurePosixPath(nfc_rel(p.relative_to(dumps_dir).as_posix()))
                if is_template_ext_xml_rel(rel):
                    n += 1
        except OSError:
            pass
    return n


def _count_dump_help_files(dumps_dir: Path, profile: dict) -> int:
    from pathlib import PurePosixPath

    from app.services.help_parser import _is_help_html_rel, _profile_flag

    if not _profile_flag(profile, "help", True):
        return 0
    n = 0
    try:
        for p in dumps_dir.rglob("*.html"):
            if not p.is_file():
                continue
            rel = PurePosixPath(nfc_rel(p.relative_to(dumps_dir).as_posix()))
            if _is_help_html_rel(rel):
                n += 1
    except OSError:
        pass
    return n


def _ordinary_forms_from_profile(profile: dict) -> bool:
    """Текст модулей обычных форм (Form.bin) — отдельный флаг профиля."""
    v = profile.get("ordinary_forms")
    return v if isinstance(v, bool) else False


def _count_dump_bsl_files(dumps_dir: Path, profile: dict, entity: dict) -> int:
    want_bsl = _bsl_enabled_from_profile(profile, entity)
    want_bin = _ordinary_forms_from_profile(profile)
    if not want_bsl and not want_bin:
        return 0
    from app.services import bsl_parser

    try:
        return sum(
            1
            for _ in bsl_parser.iter_module_files(
                dumps_dir, include_bsl=want_bsl, include_form_bin=want_bin
            )
        )
    except OSError:
        return 0

def iter_clone_entity(source_id: int, new_name: str) -> Iterator[dict[str, Any]]:
    """Deep-copy a ready entity; yield progress events ``{pct, step, detail[, new_id]}``."""
    name = (new_name or "").strip()
    if not name:
        raise ValueError("New name is required")
    conn = connect(settings.db_path)
    new_id: int | None = None
    src: dict | None = None
    try:
        yield {"pct": 3, "step": "prepare", "detail": name}
        src = ent_repo.get_entity(conn, source_id)
        if not src:
            raise ValueError("Entity not found")
        if src.get("status") != "ready":
            raise ValueError("Only fully indexed (ready) entities can be copied")
        if ent_repo.get_entity_by_name(conn, name):
            raise ValueError(f"Name already exists: {name}")

        model = src["model"] or runtime_settings.get_default_embedding_model()
        src_mode = (src.get("source_mode") or "report").strip().lower()
        src_loc = (src.get("source_location") or "upload").strip().lower()
        ingest_profile = src.get("ingest_profile") or "{}"
        if not isinstance(ingest_profile, str):
            ingest_profile = json.dumps(ingest_profile, ensure_ascii=False)

        cur = conn.execute(
            """
            INSERT INTO entities(
              name, name_locked, synonym, comment, entity_type, version, file_path,
              source_mode, source_location, source_path, ingest_profile, dumps_dir, zip_path,
              enabled, bsl_enabled, bsl_load_mode, bsl_embed_mode, model, status,
              object_count, link_count, indexed_count, index_target, index_started_at,
              parse_gen, parse_added, parse_changed, parse_deleted, parse_unchanged,
              bsl_method_count, error_message
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?,?,0,?,?,?,?,?,?, '')
            """,
            (
                name,
                1,
                src.get("synonym") or "",
                src.get("comment")
                or ent_repo.comment_from_name_version(name, src.get("version") or ""),
                src.get("entity_type") or "configuration",
                src.get("version") or "",
                "",  # file_path — after artifacts
                src_mode,
                src_loc,
                src.get("source_path") or "",
                ingest_profile,
                "",  # dumps_dir
                "",  # zip_path
                int(src.get("enabled") or 1),
                int(src["bsl_enabled"]) if src.get("bsl_enabled") is not None else 1,
                str(src.get("bsl_load_mode") or ""),
                str(src.get("bsl_embed_mode") or ""),
                model,
                int(src.get("object_count") or 0),
                int(src.get("link_count") or 0),
                int(src.get("indexed_count") or src.get("object_count") or 0),
                int(src.get("object_count") or 0),  # index_target
                int(src.get("parse_gen") or 0),
                int(src.get("parse_added") or 0),
                int(src.get("parse_changed") or 0),
                int(src.get("parse_deleted") or 0),
                int(src.get("parse_unchanged") or 0),
                int(src.get("bsl_method_count") or 0),
            ),
        )
        new_id = int(cur.lastrowid)
        yield {"pct": 8, "step": "copy_file", "detail": name}

        file_path = ""
        dumps_dir = ""
        zip_path = ""
        source_path = src.get("source_path") or ""

        if src_loc == "path":
            # Внешний источник — копия ссылается на тот же путь.
            file_path = str(src.get("file_path") or "")
            dumps_dir = str(src.get("dumps_dir") or "") if src_mode == "dump" else ""
            zip_path = ""
            if src_mode == "report" and file_path and not Path(file_path).is_file():
                raise ValueError("Source report file is missing")
            if src_mode == "dump" and dumps_dir and not Path(dumps_dir).is_dir():
                raise ValueError("Source dump directory is missing")
        elif src_mode == "dump":
            src_dump = Path(src.get("dumps_dir") or "")
            dest_dump = entity_dump_dir(new_id)
            if not src_dump.is_dir():
                raise ValueError("Source dump directory is missing")
            if dest_dump.exists():
                shutil.rmtree(dest_dump, ignore_errors=True)
            shutil.copytree(src_dump, dest_dump)
            dumps_dir = str(dest_dump.resolve())
            src_zip = Path(src.get("zip_path") or "")
            dest_zip = entity_zip_path(new_id)
            if src_zip.is_file():
                dest_zip.parent.mkdir(parents=True, exist_ok=True)
                dest_zip.write_bytes(src_zip.read_bytes())
                zip_path = str(dest_zip.resolve())
            source_path = source_path or src_dump.name
        else:
            src_file = Path(src.get("file_path") or "")
            if not src_file.is_file():
                raise ValueError("Source report file is missing")
            dest_file = entity_report_path(new_id)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_bytes(src_file.read_bytes())
            file_path = str(dest_file.resolve())
            source_path = source_path or dest_file.name

        conn.execute(
            """
            UPDATE entities SET file_path=?, dumps_dir=?, zip_path=?, source_path=?,
              updated_at=datetime('now') WHERE id=?
            """,
            (file_path, dumps_dir, zip_path, source_path, new_id),
        )

        id_map: dict[int, int] = {}
        rows = conn.execute(
            """
            SELECT id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
                   props_json, content_hash, parse_gen, embed_done, source_rel, guid
            FROM objects WHERE entity_id=?
            ORDER BY id
            """,
            (source_id,),
        ).fetchall()
        total_obj = len(rows)
        yield {"pct": 12, "step": "copy_objects", "detail": f"0 / {total_obj}"}
        for idx, r in enumerate(rows, start=1):
            guid_val = ""
            try:
                guid_val = (r["guid"] if "guid" in r.keys() else "") or ""
            except (IndexError, KeyError, TypeError):
                guid_val = ""
            cur = conn.execute(
                """
                INSERT INTO objects(
                  entity_id, path, kind_ru, kind, name, synonym, comment, belong, base_object,
                  props_json, content_hash, parse_gen, embed_done, source_rel, guid
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id
                """,
                (
                    new_id,
                    r["path"],
                    r["kind_ru"],
                    r["kind"],
                    r["name"],
                    r["synonym"] or "",
                    r["comment"] or "",
                    r["belong"] or "Own",
                    r["base_object"] or "",
                    r["props_json"] or "{}",
                    r["content_hash"] or "",
                    int(r["parse_gen"] or 0),
                    int(r["embed_done"] or 0),
                    (r["source_rel"] if "source_rel" in r.keys() else "") or "",
                    guid_val,
                ),
            )
            id_map[int(r["id"])] = int(cur.fetchone()[0])
            if idx == total_obj or idx % 250 == 0:
                pct = 12 + int(38 * idx / max(total_obj, 1))
                yield {"pct": pct, "step": "copy_objects", "detail": f"{idx} / {total_obj}"}

        yield {"pct": 55, "step": "copy_links", "detail": name}
        conn.execute(
            """
            INSERT INTO links(entity_id, from_path, to_ref, link_type)
            SELECT ?, from_path, to_ref, link_type FROM links WHERE entity_id=?
            """,
            (new_id, source_id),
        )
        dump_file_repo.copy_stamps(conn, source_id, new_id)
        from app.repositories import tags as tag_repo

        yield {"pct": 60, "step": "copy_tags", "detail": name}
        tag_repo.copy_entity_tags(conn, source_id, new_id)
        conn.commit()

        src_coll = collection_path(source_id, model)
        dst_coll = collection_path(new_id, model)
        if not src_coll.exists() or not id_map:
            ent_repo.set_status(conn, new_id, "parsed", indexed_count=0, index_target=0)
            conn.commit()
            log.warning("clone %s->%s: no zvec source, left as parsed", source_id, new_id)
            yield {
                "pct": 100,
                "step": "done",
                "detail": name,
                "new_id": new_id,
                "needs_reindex": True,
            }
            return

        yield {"pct": 65, "step": "copy_index", "detail": name}
        zvec_store.release(src_coll)
        zvec_store.release(dst_coll)
        _destroy_path(dst_coll)

        from app.services.bsl_embed import zvec_doc_ids_for_object

        src_z = zvec.open(str(src_coll), option=zvec.CollectionOption(read_only=True))
        sample_old = next(iter(id_map.keys()))
        sample = src_z.fetch(str(sample_old), include_vector=True)
        sample_doc = next(iter(sample.values()))
        dims = len(sample_doc.vectors["embedding"])
        dst_z = zvec.create_and_open(str(dst_coll), build_schema(dims))
        try:
            old_ids = list(id_map.keys())
            chunk = 100
            total_ids = len(old_ids)
            for i in range(0, total_ids, chunk):
                part = old_ids[i : i + chunk]
                fetch_ids: list[str] = []
                for x in part:
                    fetch_ids.extend(zvec_doc_ids_for_object(x))
                fetched = src_z.fetch(fetch_ids, include_vector=True)
                docs = []
                for oid_s, doc in fetched.items():
                    base_s, _, suffix = str(oid_s).partition("#")
                    try:
                        base_id = int(base_s)
                    except ValueError:
                        continue
                    nid = id_map.get(base_id)
                    if nid is None:
                        continue
                    new_doc_id = str(nid) if not suffix else f"{nid}#{suffix}"
                    docs.append(
                        zvec.Doc(
                            id=new_doc_id,
                            fields=dict(doc.fields),
                            vectors={"embedding": list(doc.vectors["embedding"])},
                        )
                    )
                if docs:
                    dst_z.insert(docs)
                done = min(i + chunk, total_ids)
                pct = 65 + int(30 * done / max(total_ids, 1))
                yield {"pct": pct, "step": "copy_index", "detail": f"{done} / {total_ids}"}
            try:
                dst_z.flush()
            except Exception:
                log.debug("flush after clone failed", exc_info=True)
        finally:
            _close_collection(dst_z)
            del src_z
            gc.collect()

        yield {"pct": 97, "step": "finalize", "detail": name}
        ent_repo.set_status(
            conn,
            new_id,
            "ready",
            indexed_count=int(src.get("object_count") or len(id_map)),
            index_target=int(src.get("object_count") or len(id_map)),
            error_message="",
        )
        conn.commit()
        log.info("cloned entity %s -> %s name=%s objects=%s", source_id, new_id, name, len(id_map))
        yield {
            "pct": 100,
            "step": "done",
            "detail": name,
            "new_id": new_id,
            "needs_reindex": False,
        }
    except Exception:
        if new_id is not None:
            try:
                coll = collection_path(
                    new_id,
                    (src or {}).get("model") or runtime_settings.get_default_embedding_model(),
                )
                zvec_store.release(coll)
                _destroy_path(coll)
                delete_entity_upload_files(new_id)
                ent_repo.delete_entity(conn, new_id)
                conn.commit()
            except Exception:
                log.exception("cleanup after failed clone %s", new_id)
        raise
    finally:
        conn.close()


def _exclude_subs_from_profile(profile: dict) -> list[str]:
    raw = profile.get("exclude_name_substrings") or []
    return [str(s).strip().lower() for s in raw if isinstance(s, str) and str(s).strip()]


def _bsl_enabled_from_profile(profile: dict, _entity: dict) -> bool:
    """Индексация BSL при parse — только по ingest-профилю (не по runtime-toggle строки)."""
    v = profile.get("bsl")
    return v if isinstance(v, bool) else True


def _bsl_load_mode_from_profile(profile: dict, entity: dict) -> str:
    """signatures_only|signatures|full (legacy code→full)."""
    from app.schemas.entities import normalize_bsl_load_mode

    for source in (profile, entity):
        raw = str(source.get("bsl_load_mode") or "").strip()
        if raw:
            return normalize_bsl_load_mode(raw)
    return "signatures"


def _report_file_unchanged(conn, entity_id: int, path: Path) -> bool:
    stamp = file_stamp(path)
    if stamp is None:
        return False
    prev = dump_file_repo.load_stamps(conn, entity_id)
    return prev.get(REPORT_FILE_REL) == stamp


def _save_report_file_stamp(conn, entity_id: int, path: Path) -> None:
    stamp = file_stamp(path)
    if stamp is None:
        return
    dump_file_repo.replace_stamps(conn, entity_id, {REPORT_FILE_REL: stamp})


def _unchanged_skip_reindex(prev_status: str, added: int, changed: int, deleted: int) -> bool:
    return (
        (prev_status or "").strip() == "ready"
        and added == 0
        and changed == 0
        and deleted == 0
    )


def parse_entity(entity_id: int) -> None:
    """Dispatch by source_mode: 'report' — текст-отчёт, 'dump' — Hierarchical-выгрузка."""
    conn = connect(settings.db_path)
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return
        source_mode = (entity.get("source_mode") or "report").strip().lower()
    finally:
        conn.close()
    if source_mode == "dump":
        _parse_dump_entity(entity_id)
    else:
        _parse_report_entity(entity_id)


def _parse_report_entity(entity_id: int) -> None:
    """Stream report nodes → SQLite in batches (no full node list / no full path-index in RAM).

    cfsmcp2 (§4): применяется ``ingest_profile.exclude_name_substrings`` —
    узел исключается, если имя (последний сегмент пути) содержит подстроку
    (регистронезависимо); исключение каскадное (все потомки тоже).
    """
    parsed_ok = False
    timer = _PhaseTimer()
    timer.mark("prep")
    conn = connect(settings.db_path)
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return
        path = Path(entity["file_path"])
        prev_status = (entity.get("status") or "").strip()
        if (
            int(entity.get("object_count") or 0) > 0
            and prev_status == "ready"
            and _report_file_unchanged(conn, entity_id, path)
        ):
            log.info("parse skipped entity=%s mode=report file unchanged", entity_id)
            timer.log("parse", entity_id, summary="skipped=unchanged", mode="report")
            return
        ent_repo.set_status(
            conn, entity_id, "parsing", error_message="", indexed_count=0, index_target=100
        )
        conn.commit()

        exclude_subs = _exclude_subs_from_profile(ent_repo.ingest_profile_of(entity))
        excluded_paths: list[str] = []

        def _is_excluded(node_path: str) -> bool:
            if any(node_path.startswith(p + ".") for p in excluded_paths):
                return True
            if exclude_subs:
                name = node_path.rsplit(".", 1)[-1].lower()
                if any(s in name for s in exclude_subs):
                    excluded_paths.append(node_path)
                    return True
            return False

        parse_gen = int(entity.get("parse_gen") or 0) + 1

        conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
        conn.commit()

        fields_batch: list[dict] = []
        new_rows: list[dict] = []
        changed_rows: list[dict] = []
        touch_ids: list[int] = []
        links_batch: list[tuple[str, str, str]] = []
        added = changed = unchanged = 0
        total_links = 0
        total_obj = 0
        excluded = 0
        meta_applied = False
        first_node = True
        decode_stats = TextDecodeStats()
        last_progress_pct = -1

        def _on_report_bytes(done: int, total: int) -> None:
            nonlocal last_progress_pct
            pct = _parse_phase_pct(1, _PARSE_FINAL_PCT, done, total)
            if pct == last_progress_pct:
                return
            last_progress_pct = pct
            _set_parse_progress(
                conn,
                entity_id,
                pct,
                object_count=total_obj,
                link_count=total_links,
                parse_added=added,
                parse_changed=changed,
                parse_unchanged=unchanged,
            )
            conn.commit()

        def flush_objects() -> None:
            nonlocal added, changed, unchanged
            if not fields_batch:
                return
            unique_batch = _dedupe_fields_by_path(fields_batch)
            existing = obj_repo.lookup_paths(
                conn, entity_id, [f["path"] for f in unique_batch]
            )
            a, c, u = _classify_and_queue(
                unique_batch,
                existing,
                new_rows=new_rows,
                changed_rows=changed_rows,
                touch_ids=touch_ids,
            )
            added += a
            changed += c
            unchanged += u
            fields_batch.clear()
            _flush_merge_batches(
                conn,
                entity_id,
                new_rows=new_rows,
                changed_rows=changed_rows,
                touch_ids=touch_ids,
                parse_gen=parse_gen,
            )
            conn.commit()

        def flush_links() -> None:
            nonlocal total_links
            if not links_batch:
                return
            total_links += obj_repo.insert_links_batch(conn, entity_id, links_batch)
            links_batch.clear()
            conn.commit()

        timer.mark("stream")
        for node in iter_report_nodes(path, decode_stats, on_progress=_on_report_bytes):
            if first_node:
                # Fallback = tentative upload name (file stem), not e{id}.txt
                meta = meta_from_first_node(node, entity["name"])
                entity_id, entity, path = _apply_report_meta(conn, entity_id, entity, meta)
                parse_gen = int(entity.get("parse_gen") or 0) + 1
                conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
                conn.commit()
                meta_applied = True
                first_node = False

            if _is_excluded(node.path):
                excluded += 1
                continue

            fields = node_fields(node)
            fields["content_hash"] = obj_repo.content_hash(fields)
            fields["parse_gen"] = parse_gen
            fields_batch.append(fields)
            links_batch.extend(extract_links(node))
            total_obj += 1

            if len(fields_batch) >= OBJECT_FLUSH:
                flush_objects()
            if len(links_batch) >= LINK_FLUSH:
                flush_links()
            if total_obj % OBJECT_FLUSH == 0:
                _set_parse_progress(
                    conn,
                    entity_id,
                    max(last_progress_pct, 1),
                    object_count=total_obj,
                    link_count=total_links,
                    parse_added=added,
                    parse_changed=changed,
                    parse_unchanged=unchanged,
                )
                conn.commit()
        if not meta_applied:
            meta = meta_from_first_node(None, entity["name"])
            entity_id, entity, path = _apply_report_meta(conn, entity_id, entity, meta)
            parse_gen = int(entity.get("parse_gen") or 0) + 1
            conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
            conn.commit()

        flush_objects()
        flush_links()

        timer.mark("cleanup")
        deleted_ids = obj_repo.delete_stale_objects(
            conn,
            entity_id,
            parse_gen,
            exclude_kinds=("Procedure", "Function"),
        )
        # BSL methods whose parent metadata object was just removed (or already missing)
        orphan_ids = obj_repo.delete_orphan_method_objects(conn, entity_id)
        deleted = len(deleted_ids) + len(orphan_ids)
        if orphan_ids:
            log.info(
                "parse entity %s: removed %s orphan BSL methods after parent delete",
                entity_id,
                len(orphan_ids),
            )

        timer.mark("finalize")
        total_stored = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=?",
                (entity_id,),
            ).fetchone()["c"]
        )

        enc_note = ""
        if decode_stats.replacement_count:
            enc_note = (
                f"encoding={decode_stats.encoding} "
                f"replacements={decode_stats.replacement_count}"
            )
            log.warning(
                "parsed entity %s with decode replacements: %s",
                entity_id,
                enc_note,
            )
        _save_report_file_stamp(conn, entity_id, path)
        skip_reindex = _unchanged_skip_reindex(prev_status, added, changed, deleted)
        ent_repo.set_status(
            conn,
            entity_id,
            "ready" if skip_reindex else "parsed",
            object_count=total_stored,
            link_count=total_links,
            parse_gen=parse_gen,
            parse_added=added,
            parse_changed=changed,
            parse_deleted=deleted,
            parse_unchanged=unchanged,
            # Soft UI signal when � appeared; kept across reindex until clean re-parse.
            error_message=enc_note,
            indexed_count=total_stored if skip_reindex else 0,
            index_target=total_stored if skip_reindex else 0,
        )
        conn.commit()
        log.info(
            "parsed entity %s objects=%s links=%s +%s ~%s -%s =%s excluded=%s encoding=%s replacements=%s",
            entity_id,
            total_obj,
            total_links,
            added,
            changed,
            deleted,
            unchanged,
            excluded,
            decode_stats.encoding,
            decode_stats.replacement_count,
        )
        timing = timer.summary()
        timer.log("parse", entity_id, summary=timing, mode="report", objects=total_stored)
        _persist_index_job(
            name="parse",
            entity=entity,
            timer=timer,
            scale={
                "mode": "report",
                "objects": total_obj,
                "added": added,
                "changed": changed,
                "deleted": deleted,
                "unchanged": unchanged,
                "excluded": excluded,
            },
        )
        usage_stats.record(
            kind="index",
            name="parse",
            ok=True,
            duration_ms=timer.snapshot()["duration_ms"],
            detail=(
                f"objects={total_obj} +{added}~{changed}-{deleted}={unchanged} "
                f"excluded={excluded} enc={decode_stats.encoding} repl={decode_stats.replacement_count} "
                f"{timing}"
            ),
            tier="usage",
            persist=False,
        )
        parsed_ok = not skip_reindex
    except Exception as exc:
        log.exception("parse failed entity=%s", entity_id)
        try:
            ent_repo.set_status(conn, entity_id, "parse_error", error_message=str(exc))
            conn.commit()
        except Exception:
            log.exception("failed to persist parse_error for entity=%s", entity_id)
        usage_stats.record(
            kind="index", name="parse", ok=False, detail=str(exc)[:200], tier="usage", persist=False
        )
        usage_stats.record_error_detail(
            kind="index",
            name="parse",
            context=str(entity_id),
            detail=str(exc)[:2000],
        )
    finally:
        conn.close()

    if parsed_ok:
        log.info("auto-reindex after parse entity=%s", entity_id)
        reindex_entity(entity_id)


def _parse_dump_entity(entity_id: int) -> None:
    """Stream dump metadata objects → SQLite (этап 3, ТЗ §7.2).

    Те же merge-функции и exclude_name_substrings, что в report-режиме; объекты
    приходят из DumpScanner (streaming по объектным XML). Ведётся dump audit:
    файлы-кандидаты без объектов → ``metadata_dir/e{id}.dump.audit.jsonl``.
    """
    from app.services.dump_parser import DumpScanner, read_configuration_meta

    parsed_ok = False
    timer = _PhaseTimer()
    timer.mark("prep")
    conn = connect(settings.db_path)
    try:
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return
        prev_status = (entity.get("status") or "").strip()
        ent_repo.set_status(
            conn, entity_id, "parsing", error_message="", indexed_count=0, index_target=100
        )
        conn.commit()
        dumps_dir = Path(entity.get("dumps_dir") or "")
        if not dumps_dir.is_dir():
            raise RuntimeError(f"dump directory not found: {dumps_dir}")
        from app.services.dump_zip import resolve_dump_root

        dumps_dir = resolve_dump_root(dumps_dir)
        if not (dumps_dir / "Configuration.xml").is_file():
            raise RuntimeError(
                f"Configuration.xml not found under dump root: {dumps_dir}"
            )

        exclude_subs = _exclude_subs_from_profile(ent_repo.ingest_profile_of(entity))
        excluded_paths: list[str] = []

        def _is_excluded(node_path: str) -> bool:
            if any(node_path.startswith(p + ".") for p in excluded_paths):
                return True
            if exclude_subs:
                name = node_path.rsplit(".", 1)[-1].lower()
                if any(s in name for s in exclude_subs):
                    excluded_paths.append(node_path)
                    return True
            return False

        meta = read_configuration_meta(dumps_dir)
        entity_id, entity, path = _apply_dump_meta(conn, entity_id, entity, meta)
        parse_gen = int(entity.get("parse_gen") or 0) + 1

        prev_stamps = dump_file_repo.load_stamps(conn, entity_id)
        if tracked_files_unchanged(dumps_dir, prev_stamps):
            timer.mark("stamp")
            unchanged = obj_repo.touch_all_objects_parse_gen(conn, entity_id, parse_gen)
            dump_file_repo.replace_stamps(conn, entity_id, prev_stamps)
            total_stored = int(entity.get("object_count") or unchanged)
            total_links = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM links WHERE entity_id=?",
                    (entity_id,),
                ).fetchone()["c"]
            )
            skip_reindex = _unchanged_skip_reindex(prev_status, 0, 0, 0)
            bsl_method_count = int(entity.get("bsl_method_count") or 0)
            bsl_load_mode = _bsl_load_mode_from_profile(
                ent_repo.ingest_profile_of(entity), entity
            )
            ent_repo.set_status(
                conn,
                entity_id,
                "ready" if skip_reindex else "parsed",
                object_count=total_stored,
                link_count=total_links,
                parse_gen=parse_gen,
                parse_added=0,
                parse_changed=0,
                parse_deleted=0,
                parse_unchanged=unchanged,
                bsl_load_mode=bsl_load_mode,
                bsl_method_count=bsl_method_count,
                indexed_count=total_stored if skip_reindex else 0,
                index_target=total_stored if skip_reindex else 0,
            )
            conn.commit()
            log.info(
                "dump parsed entity %s objects=%s links=%s +0 ~0 -0 =%s "
                "files_skipped=%s files_parsed=0 (all tracked unchanged)",
                entity_id,
                total_stored,
                total_links,
                unchanged,
                len(prev_stamps),
            )
            timing = timer.summary()
            timer.log(
                "parse",
                entity_id,
                summary=timing,
                mode="dump",
                objects=total_stored,
                skipped=len(prev_stamps),
                parsed_files=0,
            )
            usage_stats.record(
                kind="index",
                name="parse",
                ok=True,
                duration_ms=timer.snapshot()["duration_ms"],
                detail=(
                    f"mode=dump objects={total_stored} +0~0-0={unchanged} "
                    f"skipped_files={len(prev_stamps)} parsed_files=0 all_unchanged "
                    f"{timing}"
                ),
                tier="usage",
                persist=False,
            )
            _persist_index_job(
                name="parse",
                entity=entity,
                timer=timer,
                scale={
                    "mode": "dump",
                    "objects": total_stored,
                    "unchanged": unchanged,
                    "skipped_files": len(prev_stamps),
                    "parsed_files": 0,
                },
                note="all_unchanged",
                skip_if_short=True,
            )
            parsed_ok = not skip_reindex
        else:
            tracker = DumpFileTracker(dumps_dir, prev_stamps)
            incremental_links = bool(prev_stamps)
            if not incremental_links:
                conn.execute("DELETE FROM links WHERE entity_id=?", (entity_id,))
                conn.commit()
            links_cleared: set[str] = set()
            help_owners_parsed: set[str] = set()

            audit_path = entity_dump_audit_path(entity_id)
            audit_path.unlink(missing_ok=True)

            fields_batch: list[dict] = []
            new_rows: list[dict] = []
            changed_rows: list[dict] = []
            touch_ids: list = []
            links_batch: list[tuple[str, str, str]] = []
            added = changed = unchanged = 0
            total_links = 0
            total_obj = 0
            excluded = 0
            last_progress_pct = -1

            def _emit_pct(pct: int) -> None:
                nonlocal last_progress_pct
                pct = max(0, min(99, int(pct)))
                if pct == last_progress_pct:
                    return
                last_progress_pct = pct
                _set_parse_progress(
                    conn,
                    entity_id,
                    pct,
                    object_count=total_obj,
                    link_count=total_links,
                    parse_added=added,
                    parse_changed=changed,
                    parse_unchanged=unchanged,
                )
                conn.commit()

            _emit_pct(1)
            profile = ent_repo.ingest_profile_of(entity)
            timer.mark("walk")
            sidecars = collect_sidecars(dumps_dir, profile)
            counts = dump_file_repo.classify_counts(prev_stamps)
            if prev_stamps:
                timer.mark("count")
                meta_total = max(counts["meta"], 1)
                asset_total = counts["assets"]
                help_total = counts["help"]
                bsl_total = counts["bsl"]
            else:
                timer.mark("count")
                meta_total = _count_dump_meta_files(dumps_dir)
                asset_total = len(sidecars.forms) + len(sidecars.templates)
                help_total = len(sidecars.help_html)
                bsl_total = len(sidecars.modules)

            def flush_objects() -> None:
                nonlocal added, changed, unchanged
                if not fields_batch:
                    return
                unique_batch = _dedupe_fields_by_path(fields_batch)
                existing = obj_repo.lookup_paths(
                    conn, entity_id, [f["path"] for f in unique_batch]
                )
                a, c, u = _classify_and_queue(
                    unique_batch,
                    existing,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    touch_ids=touch_ids,
                )
                added += a
                changed += c
                unchanged += u
                fields_batch.clear()
                _flush_merge_batches(
                    conn,
                    entity_id,
                    new_rows=new_rows,
                    changed_rows=changed_rows,
                    touch_ids=touch_ids,
                    parse_gen=parse_gen,
                )
                conn.commit()

            def flush_links() -> None:
                nonlocal total_links
                if not links_batch:
                    return
                total_links += obj_repo.insert_links_batch(conn, entity_id, links_batch)
                links_batch.clear()
                conn.commit()

            def _clear_meta_links(rel: str) -> None:
                if not incremental_links or not rel or rel in links_cleared:
                    return
                obj_repo.delete_links_for_source_rels(
                    conn, entity_id, [rel], exclude_types=("help", "calls")
                )
                links_cleared.add(rel)

            timer.mark("meta")
            scanner = DumpScanner(dumps_dir)
            for record, links in scanner.iter_records(should_parse=tracker.observe):
                if _is_excluded(record["path"]):
                    excluded += 1
                    continue
                rel = record.get("source_rel") or ""
                _clear_meta_links(rel)
                record["content_hash"] = obj_repo.content_hash(record)
                record["parse_gen"] = parse_gen
                fields_batch.append(record)
                links_batch.extend(links)
                total_obj += 1

                if len(fields_batch) >= OBJECT_FLUSH:
                    flush_objects()
                if len(links_batch) >= LINK_FLUSH:
                    flush_links()
                if total_obj % OBJECT_FLUSH == 0:
                    _emit_pct(
                        _parse_phase_pct(1, _PARSE_META_HI, scanner.files_seen, meta_total)
                    )

            flush_objects()
            flush_links()
            _emit_pct(_PARSE_META_HI)

            # --- Формы / СКД / XML-макеты (этап 6, ТЗ §7.2 п.6) ---
            from app.services.dump_assets_parser import iter_dump_asset_records

            timer.mark("assets")
            asset_counts = {"ManagedForm": 0, "DataCompositionSchema": 0, "SpreadsheetTemplate": 0}
            asset_done = 0
            for record in iter_dump_asset_records(
                dumps_dir,
                profile,
                should_parse=tracker.observe,
                form_files=sidecars.forms,
                template_files=sidecars.templates,
            ):
                if _is_excluded(record["path"]):
                    excluded += 1
                    continue
                record["content_hash"] = obj_repo.content_hash(record)
                record["parse_gen"] = parse_gen
                fields_batch.append(record)
                total_obj += 1
                k = record.get("kind") or ""
                if k in asset_counts:
                    asset_counts[k] += 1
                asset_done += 1
                if len(fields_batch) >= OBJECT_FLUSH:
                    flush_objects()
                if asset_done % 50 == 0 or asset_done == asset_total:
                    _emit_pct(
                        _parse_phase_pct(
                            _PARSE_META_HI, _PARSE_ASSETS_HI, asset_done, max(asset_total, 1)
                        )
                    )
            flush_objects()
            _emit_pct(_PARSE_ASSETS_HI if asset_total else _PARSE_META_HI)

            # --- Пользовательская справка (U10): **/Ext/Help/*.html → kind=Help ---
            from app.services.help_parser import iter_dump_help_records

            timer.mark("help")
            help_count = 0
            help_lo = _PARSE_ASSETS_HI if asset_total else _PARSE_META_HI
            for record in iter_dump_help_records(
                dumps_dir,
                profile,
                should_parse=tracker.observe,
                files=sidecars.help_html,
            ):
                if _is_excluded(record["path"]):
                    excluded += 1
                    continue
                owner = record["props"].get("owner") or ""
                if owner and owner not in help_owners_parsed:
                    help_owners_parsed.add(owner)
                    if incremental_links:
                        obj_repo.delete_help_links_for_owners(conn, entity_id, [owner])
                    links_batch.append((owner, f"Help.{owner}", "help"))
                record["content_hash"] = obj_repo.content_hash(record)
                record["parse_gen"] = parse_gen
                fields_batch.append(record)
                help_count += 1
                total_obj += 1
                if len(fields_batch) >= OBJECT_FLUSH:
                    flush_objects()
                if len(links_batch) >= LINK_FLUSH:
                    flush_links()
                if help_count % 50 == 0 or help_count == help_total:
                    _emit_pct(
                        _parse_phase_pct(help_lo, _PARSE_HELP_HI, help_count, max(help_total, 1))
                    )
            flush_objects()
            flush_links()
            _emit_pct(_PARSE_HELP_HI if help_total else help_lo)

            # --- BSL / Form.bin (этап 4): *.bsl по bsl, Form.bin по ordinary_forms ---
            bsl_enabled_flag = _bsl_enabled_from_profile(profile, entity)
            ordinary_forms_flag = _ordinary_forms_from_profile(profile)
            modules_enabled = bsl_enabled_flag or ordinary_forms_flag
            bsl_load_mode = _bsl_load_mode_from_profile(profile, entity)
            bsl_stats = {
                "modules": 0,
                "methods": 0,
                "links": 0,
                "ambiguous": 0,
                "unresolved": 0,
            }
            calls_pending: list[tuple[str, str]] = []
            bsl_lo = _PARSE_HELP_HI if help_total else help_lo
            parsed_bsl_rels: list[str] = []
            timer.mark("bsl")
            if modules_enabled:
                def _on_bsl_module(done: int, total: int) -> None:
                    _emit_pct(_parse_phase_pct(bsl_lo, _PARSE_BSL_HI, done, total))

                bsl_stats, calls_pending, parsed_bsl_rels = _parse_dump_bsl(
                    conn,
                    entity_id,
                    parse_gen,
                    dumps_dir,
                    load_mode=bsl_load_mode,
                    exclude_subs=exclude_subs,
                    fields_batch=fields_batch,
                    flush_objects=flush_objects,
                    on_module=_on_bsl_module,
                    module_total=bsl_total,
                    include_bsl=bsl_enabled_flag,
                    include_form_bin=ordinary_forms_flag,
                    should_parse=tracker.observe,
                    files=sidecars.modules,
                )
            _emit_pct(_PARSE_BSL_HI if modules_enabled else bsl_lo)
            _emit_pct(_PARSE_FINAL_PCT)

            timer.mark("cleanup")
            if tracker.skipped:
                unchanged += obj_repo.touch_objects_by_source_rels(
                    conn, entity_id, tracker.skipped, parse_gen
                )
            deleted_ids = obj_repo.delete_stale_objects(conn, entity_id, parse_gen)
            orphan_ids = obj_repo.delete_orphan_method_objects(conn, entity_id)
            deleted = len(deleted_ids) + len(orphan_ids)
            obj_repo.cleanup_orphan_links(conn, entity_id)

            timer.mark("calls")
            if modules_enabled:
                if incremental_links and parsed_bsl_rels:
                    obj_repo.delete_links_for_source_rels(
                        conn, entity_id, parsed_bsl_rels, link_types=("calls",)
                    )
                if calls_pending:
                    call_stats = _insert_bsl_call_links(conn, entity_id, calls_pending)
                    bsl_stats["links"] = call_stats["links"]
                    bsl_stats["ambiguous"] = call_stats["ambiguous"]
                    bsl_stats["unresolved"] = call_stats["unresolved"]
                    total_links += int(call_stats["links"])
                conn.commit()

            timer.mark("finalize")
            audit_entries = scanner.audit
            if audit_entries:
                lines = [
                    json.dumps(
                        {"path": a.path, "reason": a.reason, "detail": a.detail},
                        ensure_ascii=False,
                    )
                    for a in audit_entries
                ]
                audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                log.info(
                    "dump audit entity=%s entries=%s (см. %s)",
                    entity_id,
                    len(audit_entries),
                    audit_path.name,
                )

            dump_file_repo.replace_stamps(conn, entity_id, tracker.seen)
            total_links = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM links WHERE entity_id=?",
                    (entity_id,),
                ).fetchone()["c"]
            )
            total_stored = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM objects WHERE entity_id=?",
                    (entity_id,),
                ).fetchone()["c"]
            )
            bsl_method_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? "
                    "AND kind IN ('Procedure','Function')",
                    (entity_id,),
                ).fetchone()["c"]
            )

            skip_reindex = _unchanged_skip_reindex(prev_status, added, changed, deleted)
            ent_repo.set_status(
                conn,
                entity_id,
                "ready" if skip_reindex else "parsed",
                object_count=total_stored,
                link_count=total_links,
                parse_gen=parse_gen,
                parse_added=added,
                parse_changed=changed,
                parse_deleted=deleted,
                parse_unchanged=unchanged,
                bsl_load_mode=bsl_load_mode,
                bsl_method_count=bsl_method_count,
                indexed_count=total_stored if skip_reindex else 0,
                index_target=total_stored if skip_reindex else 0,
            )
            conn.commit()
            log.info(
                "dump parsed entity %s objects=%s links=%s +%s ~%s -%s =%s excluded=%s "
                "files_skipped=%s files_parsed=%s "
                "forms=%s dcs=%s spreadsheets=%s help=%s "
                "bsl_modules=%s bsl_methods=%s bsl_calls=%s bsl_ambiguous=%s bsl_unresolved=%s audit=%s",
                entity_id,
                total_obj,
                total_links,
                added,
                changed,
                deleted,
                unchanged,
                excluded,
                len(tracker.skipped),
                len(tracker.parsed),
                asset_counts["ManagedForm"],
                asset_counts["DataCompositionSchema"],
                asset_counts["SpreadsheetTemplate"],
                help_count,
                bsl_stats["modules"],
                bsl_stats["methods"],
                bsl_stats["links"],
                bsl_stats["ambiguous"],
                bsl_stats["unresolved"],
                len(audit_entries),
            )
            timing = timer.summary()
            timer.log(
                "parse",
                entity_id,
                summary=timing,
                mode="dump",
                objects=total_stored,
                bsl_mode=bsl_load_mode,
                skipped=len(tracker.skipped),
                parsed_files=len(tracker.parsed),
            )
            usage_stats.record(
                kind="index",
                name="parse",
                ok=True,
                duration_ms=timer.snapshot()["duration_ms"],
                detail=(
                    f"mode=dump objects={total_obj} +{added}~{changed}-{deleted}={unchanged} "
                    f"excluded={excluded} skipped_files={len(tracker.skipped)} "
                    f"parsed_files={len(tracker.parsed)} "
                    f"forms={asset_counts['ManagedForm']} "
                    f"dcs={asset_counts['DataCompositionSchema']} "
                    f"spreadsheets={asset_counts['SpreadsheetTemplate']} "
                    f"help={help_count} "
                    f"bsl_modules={bsl_stats['modules']} "
                    f"bsl_methods={bsl_stats['methods']} bsl_calls={bsl_stats['links']} "
                    f"bsl_ambiguous={bsl_stats['ambiguous']} audit={len(audit_entries)} "
                    f"{timing}"
                ),
                tier="usage",
                persist=False,
            )
            _persist_index_job(
                name="parse",
                entity=entity,
                timer=timer,
                scale={
                    "mode": "dump",
                    "objects": total_obj,
                    "added": added,
                    "changed": changed,
                    "deleted": deleted,
                    "unchanged": unchanged,
                    "excluded": excluded,
                    "parsed_files": len(tracker.parsed),
                    "skipped_files": len(tracker.skipped),
                    "bsl_methods": bsl_stats["methods"],
                },
                note=f"bsl_mode={bsl_load_mode}" if bsl_load_mode else "",
            )
            parsed_ok = not skip_reindex
    except Exception as exc:
        log.exception("dump parse failed entity=%s", entity_id)
        try:
            ent_repo.set_status(conn, entity_id, "parse_error", error_message=str(exc))
            conn.commit()
        except Exception:
            log.exception("failed to persist parse_error for entity=%s", entity_id)
        usage_stats.record(
            kind="index", name="parse", ok=False, detail=str(exc)[:200], tier="usage", persist=False
        )
        usage_stats.record_error_detail(
            kind="index",
            name="parse",
            context=str(entity_id),
            detail=str(exc)[:2000],
        )
    finally:
        conn.close()

    if parsed_ok:
        log.info("auto-reindex after dump parse entity=%s", entity_id)
        reindex_entity(entity_id)


def _insert_bsl_call_links(
    conn,
    entity_id: int,
    calls_batch: list[tuple[str, str]],
) -> dict[str, int]:
    """Резолв CALLS после orphan-cleanup; только живые методы в SQLite."""
    rows = conn.execute(
        """
        SELECT path, name FROM objects
        WHERE entity_id=? AND kind IN ('Procedure','Function')
        """,
        (entity_id,),
    ).fetchall()
    living = {str(r["path"]) for r in rows}
    name_map: dict[str, set[str]] = {}
    for r in rows:
        name_map.setdefault(str(r["name"]), set()).add(str(r["path"]))

    resolved: list[tuple[str, str, str]] = []
    ambiguous = unresolved = 0
    for from_path, callee in calls_batch:
        if from_path not in living:
            continue
        targets = name_map.get(callee)
        if not targets:
            unresolved += 1
            continue
        if len(targets) > 1:
            ambiguous += 1
            continue
        to_ref = next(iter(targets))
        if to_ref not in living or to_ref == from_path:
            continue
        resolved.append((from_path, to_ref, "calls"))

    links_total = 0
    if resolved:
        chunk = 500
        for i in range(0, len(resolved), chunk):
            links_total += obj_repo.insert_links_batch(
                conn, entity_id, resolved[i : i + chunk]
            )
    if ambiguous or unresolved:
        log.info(
            "bsl calls entity=%s total=%s resolved=%s ambiguous=%s unresolved=%s",
            entity_id,
            len(calls_batch),
            links_total,
            ambiguous,
            unresolved,
        )
    return {
        "links": links_total,
        "ambiguous": ambiguous,
        "unresolved": unresolved,
    }


def _parse_dump_bsl(
    conn,
    entity_id: int,
    parse_gen: int,
    dumps_dir: Path,
    *,
    load_mode: str,
    exclude_subs: list[str],
    fields_batch: list[dict],
    flush_objects: Callable[[], None],
    on_module: Callable[[int, int], None] | None = None,
    module_total: int = 0,
    include_bsl: bool = True,
    include_form_bin: bool = False,
    should_parse: Callable[[Path], bool] | None = None,
    files: list[Path] | None = None,
) -> tuple[dict[str, int], list[tuple[str, str]], list[str]]:
    """BSL-методы из dump (этап 4): модули → методы (merge); CALLS — после orphan-cleanup.

    CALLS извлекаются при ``signatures`` и ``full`` (не при ``signatures_only``).
    ``signatures``: тело не в индексе, только рёбра. ``full``: тела + region.
    ``fields_batch`` / ``flush_objects`` — общая очередь merge с metadata-парсером.
    ``include_bsl`` / ``include_form_bin`` — какие файлы обходить.
    Возвращает (статистика, calls_pending, parsed_rels) для ``_insert_bsl_call_links``.
    """
    from app.services import bsl_parser

    module_count = 0
    method_count = 0
    calls_batch: list[tuple[str, str]] = []
    parsed_rels: list[str] = []
    total = max(1, int(module_total or 0))
    for f in bsl_parser.iter_module_files(
        dumps_dir,
        include_bsl=include_bsl,
        include_form_bin=include_form_bin,
        files=files,
    ):
        rel = nfc_rel(f.relative_to(dumps_dir).as_posix())
        parent, role = bsl_parser.classify_module_path(rel)
        if not parent or not role:
            continue
        if exclude_subs and any(
            s in parent.rsplit(".", 1)[-1].lower() for s in exclude_subs
        ):
            continue
        if should_parse is not None and not should_parse(f):
            continue
        if f.name.lower() == "form.bin":
            from app.services.form_bin import form_bin_to_module_path

            try:
                synth = form_bin_to_module_path(rel)
            except ValueError:
                continue
            if dumps_dir.joinpath(*synth.split("/")).is_file():
                continue
        parsed_rels.append(rel)
        data = f.read_bytes()
        if f.name.lower() == "form.bin":
            text = bsl_parser.extract_bsl_from_form_bin(data)
            if text is None:
                log.debug("Form.bin без распознаваемого BSL: %s", rel)
                continue
        else:
            text, _enc = bsl_parser.decode_bsl(data)
        if not text.strip():
            continue
        module_count += 1
        for m in bsl_parser.parse_module(text, parent, role, rel, load_mode, exclude_subs):
            m["source_rel"] = rel
            m["content_hash"] = obj_repo.content_hash(m)
            m["parse_gen"] = parse_gen
            fields_batch.append(m)
            method_count += 1
            for callee in m.get("calls") or []:
                calls_batch.append((m["path"], callee))
            if len(fields_batch) >= OBJECT_FLUSH:
                flush_objects()
        if on_module and (module_count % 25 == 0 or module_count >= total):
            on_module(module_count, total)

    flush_objects()
    if on_module and module_count:
        on_module(module_count, max(total, module_count))

    return (
        {
            "modules": module_count,
            "methods": method_count,
            "links": 0,
            "ambiguous": 0,
            "unresolved": 0,
        },
        calls_batch,
        parsed_rels,
    )


def _make_docs(passages: list[dict], vectors: list) -> list:
    docs = []
    for p, vec in zip(passages, vectors):
        o = p["obj"]
        docs.append(
            zvec.Doc(
                id=str(p["doc_id"]),
                fields={
                    "path": o["path"],
                    "kind": o["kind"],
                    "belong": o["belong"],
                    "name": o["name"],
                    "synonym": o["synonym"],
                    "text": p["text"],
                },
                vectors={"embedding": vec},
            )
        )
    return docs


def _embed_chunk(
    chunk_rows: list,
    model: str,
    base_url: str | None,
    mode: str,
) -> tuple[list[dict], list[dict], list[list[float]]]:
    """Embed one batch in a worker thread (own EmbeddingClient)."""
    from app.services.bsl_embed import passages_for_object

    objs = [dict(r) for r in chunk_rows]
    passages: list[dict] = []
    for o in objs:
        for doc_id, text in passages_for_object(o, mode):
            passages.append({"obj": o, "doc_id": doc_id, "text": text})
    if not passages:
        return objs, [], []
    client = EmbeddingClient(base_url=base_url)
    vectors = client.embed([p["text"] for p in passages], model, for_query=False)
    return objs, passages, vectors


def _embed_pending_into(
    conn,
    client: EmbeddingClient,
    entity_id: int,
    model: str,
    collection,
    *,
    use_upsert: bool,
) -> tuple[int, float, float, float, float]:
    """Embed pending objects; overlap LM Studio with zvec/SQLite writes.

    Returns ``(indexed_objects, embed_sec, upsert_sec, stale_delete_sec, wave_flush_sec)``.
    """
    from app.services.bsl_embed import get_bsl_embed_limits, zvec_stale_chunk_ids

    indexed_wrote = 0
    embed_sec = 0.0
    upsert_sec = 0.0
    stale_delete_sec = 0.0
    wave_flush_sec = 0.0
    batch_size = max(1, int(runtime_settings.get_embedding_batch_size()))
    workers = max(1, int(runtime_settings.get_embedding_workers()))
    overlap = runtime_settings.get_embed_pipeline_overlap()
    mode = runtime_settings.get_bsl_embed_mode()
    entity_row = ent_repo.get_entity(conn, entity_id)
    ctx_name = str((entity_row or {}).get("name") or entity_id)
    if entity_row:
        from app.services.bsl_embed import normalize_bsl_embed_mode

        mode = normalize_bsl_embed_mode(
            str(entity_row.get("bsl_embed_mode") or "") or mode
        )
    base_url = client.base_url
    max_chunks = int(get_bsl_embed_limits().get("max_chunks") or 12)
    bsl_embed_gen = int((entity_row or {}).get("bsl_embed_gen") or 0)
    scope = str((entity_row or {}).get("index_scope") or "")
    already, _ = obj_repo.count_embed_progress(conn, entity_id, scope=scope)
    wave = batch_size * workers
    last_id = 0
    pool_workers = min(max(workers, 1), max(runtime_settings.ALLOWED_EMBEDDING_WORKERS))
    log.info(
        "embed pending entity=%s workers=%s batch=%s overlap=%s wave=%s mode=%s already=%s",
        entity_id,
        workers,
        batch_size,
        overlap,
        wave,
        mode,
        already,
    )

    def fetch_wave() -> list:
        # Overlap is safe because this cursor is id-only: wave N+1 is fetched
        # before wave N is marked embed_done. Filtering on a freshly committed
        # embed_done would skip or double-fetch under overlap.
        nonlocal last_id
        rows = conn.execute(
            """
            SELECT id, path, kind, name, synonym, comment, belong, props_json
            FROM objects WHERE entity_id=? AND id>? AND (
              embed_done=0
              OR (kind IN ('Procedure','Function') AND bsl_embed_gen < ?)
            )
            ORDER BY id LIMIT ?
            """,
            (entity_id, last_id, bsl_embed_gen, wave),
        ).fetchall()
        if rows:
            last_id = int(rows[-1]["id"])
        return rows

    def start_embed(rows: list):
        if not rows:
            return [], 0.0, None
        chunks = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
        t0 = time.perf_counter()
        futs = [pool.submit(_embed_chunk, c, model, base_url, mode) for c in chunks]

        class _Wait:
            def result(self):
                return [f.result() for f in futs]
        return chunks, t0, _Wait()

    def write_results(results: list, rows: list, chunks_n: int, loop_t0: float) -> None:
        nonlocal indexed_wrote, upsert_sec, stale_delete_sec, wave_flush_sec
        stale_ids: list[str] = []
        pending_docs: list = []
        pending_obj_ids: list[int] = []
        empty_obj_ids: list[int] = []
        for objs, passages, vectors in results:
            if not objs:
                continue
            kept: dict[int, list[str]] = {}
            for p in passages:
                kept.setdefault(int(p["obj"]["id"]), []).append(str(p["doc_id"]))
            docs = _make_docs(passages, vectors)
            for o in objs:
                oid = int(o["id"])
                kept_ids = kept.get(oid, [])
                if use_upsert:
                    # Leftover oid#N from a previous chunks run — only this wave.
                    stale_ids.extend(
                        zvec_stale_chunk_ids(oid, kept_ids, max_chunks=max_chunks)
                    )
            if not docs:
                empty_obj_ids.extend(int(o["id"]) for o in objs)
                continue
            pending_docs.extend(docs)
            pending_obj_ids.extend(int(o["id"]) for o in objs)
        # Upsert first so a crash never leaves this wave without vectors.
        if pending_docs:
            t_up = time.perf_counter()
            # zvec write path rejects batches >1024 docs; keep a safety margin.
            write_batch_size = 1000
            for i in range(0, len(pending_docs), write_batch_size):
                batch_docs = pending_docs[i : i + write_batch_size]
                if use_upsert:
                    collection.upsert(batch_docs)
                else:
                    collection.insert(batch_docs)
            upsert_sec += time.perf_counter() - t_up
        if stale_ids:
            t_stale = time.perf_counter()
            for i in range(0, len(stale_ids), 500):
                try:
                    collection.delete(stale_ids[i : i + 500])
                except Exception:
                    log.debug("zvec stale chunk delete failed", exc_info=True)
            stale_delete_sec += time.perf_counter() - t_stale
        if pending_docs or empty_obj_ids:
            t_flush = time.perf_counter()
            try:
                collection.flush()
            except Exception:
                log.warning(
                    "zvec flush before mark failed entity=%s; wave stays pending",
                    entity_id,
                    exc_info=True,
                )
                raise
            wave_flush_sec += time.perf_counter() - t_flush
            if pending_obj_ids:
                obj_repo.mark_embedded(conn, pending_obj_ids, bsl_embed_gen=bsl_embed_gen)
                indexed_wrote += len(pending_obj_ids)
            if empty_obj_ids:
                obj_repo.mark_embedded(conn, empty_obj_ids, bsl_embed_gen=bsl_embed_gen)
                indexed_wrote += len(empty_obj_ids)
        ent_repo.set_status(
            conn, entity_id, "indexing", indexed_count=already + indexed_wrote
        )
        conn.commit()
        last_row = rows[-1] if rows else None
        usage_stats.note_index_progress(
            context=ctx_name,
            role="index",
            model=model,
            objects=sum(len(objs) for objs, _, _ in results),
            passages=sum(len(p) for _, p, _ in results),
            batches=chunks_n,
            duration_ms=(time.perf_counter() - loop_t0) * 1000,
            last_path=str(last_row["path"] if last_row else ""),
            last_kind=str(last_row["kind"] if last_row else ""),
            last_name=str(last_row["name"] if last_row else ""),
        )

    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        if overlap:
            current_rows = fetch_wave()
            chunks, t_embed0, embed_wait = start_embed(current_rows)
            while current_rows:
                loop_t0 = t_embed0 or time.perf_counter()
                nxt = fetch_wave()
                results = embed_wait.result() if embed_wait is not None else []
                embed_sec += time.perf_counter() - t_embed0
                n_chunks, t_next0, next_wait = start_embed(nxt)
                write_results(results, current_rows, len(chunks), loop_t0)
                current_rows = nxt
                chunks = n_chunks
                t_embed0 = t_next0
                embed_wait = next_wait
        else:
            current_rows = fetch_wave()
            while current_rows:
                chunks, t_embed0, embed_wait = start_embed(current_rows)
                loop_t0 = t_embed0 or time.perf_counter()
                results = embed_wait.result() if embed_wait is not None else []
                embed_sec += time.perf_counter() - t_embed0
                write_results(results, current_rows, len(chunks), loop_t0)
                current_rows = fetch_wave()

    usage_stats.flush_index_progress()
    return indexed_wrote, embed_sec, upsert_sec, stale_delete_sec, wave_flush_sec


def _reindex_incremental(
    conn,
    client: EmbeddingClient,
    entity_id: int,
    model: str,
    coll_path: Path,
    pending_deletes: list[str],
) -> tuple[int, float, float, float, float, float, float]:
    """Upsert changed docs and delete removed ids in the existing collection.

    Returns ``(indexed, setup_sec, embed_sec, upsert_sec, stale_delete_sec,
    wave_flush_sec, zvec_flush_sec)``.
    """
    t_setup = time.perf_counter()
    zvec_store.release(coll_path)
    try:
        cleanup_zvec_crash_residue(coll_path)
    except Exception:
        log.debug("zvec residue cleanup before incremental failed", exc_info=True)
    collection = zvec_store.get(coll_path, read_only=False)
    try:
        zvec_del_sec = 0.0
        if pending_deletes:
            t_del = time.perf_counter()
            chunk = 500
            for i in range(0, len(pending_deletes), chunk):
                collection.delete(pending_deletes[i : i + chunk])
            obj_repo.clear_pending_zvec_deletes(conn, entity_id)
            conn.commit()
            zvec_del_sec = time.perf_counter() - t_del
        setup_sec = time.perf_counter() - t_setup
        indexed, embed_sec, upsert_sec, stale_delete_sec, wave_flush_sec = _embed_pending_into(
            conn, client, entity_id, model, collection, use_upsert=True
        )
        stale_delete_sec += zvec_del_sec
        t_flush = time.perf_counter()
        try:
            collection.flush()
        except Exception:
            log.debug("flush after incremental reindex failed", exc_info=True)
        try:
            if hasattr(collection, "optimize"):
                collection.optimize()
        except Exception:
            log.debug("optimize after incremental reindex failed", exc_info=True)
        zvec_flush_sec = time.perf_counter() - t_flush
        return (
            indexed,
            setup_sec,
            embed_sec,
            upsert_sec,
            stale_delete_sec,
            wave_flush_sec,
            zvec_flush_sec,
        )
    finally:
        zvec_store.release(coll_path)


def _normalize_index_scope(scope: str | None) -> str:
    s = (scope or "").strip().lower()
    if s in {"report", "bsl", "all"}:
        return s
    return ""


def _infer_index_scope(
    conn,
    entity_id: int,
    *,
    full_rebuild: bool,
) -> str:
    """report = metadata objects, bsl = methods, all = both."""
    if full_rebuild:
        meta, methods = obj_repo.count_objects_split(conn, entity_id)
    else:
        meta, methods = obj_repo.count_embed_pending_split(conn, entity_id)
    if methods and meta:
        return "all"
    if methods:
        return "bsl"
    return "report"


_reindex_lock_guard = threading.Lock()
_reindex_locks: dict[int, threading.Lock] = {}


def _try_acquire_reindex_lock(entity_id: int) -> threading.Lock | None:
    """Non-blocking per-entity lock so resume=True cannot start a second in-process worker.

    A failed acquire is safe (no second writer) but not a catch-up: the winner's
    id cursor only moves forward, so rows marked pending behind it wait until
    the next explicit /reindex.
    """
    with _reindex_lock_guard:
        lock = _reindex_locks.setdefault(int(entity_id), threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    return lock


def recover_interrupted_indexing() -> int:
    """After process restart, status=indexing is a stale lock — resume remaining embeds."""
    from app.services import jobs

    conn = connect(settings.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, name, indexed_count, index_target, object_count
            FROM entities WHERE status='indexing'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    n = 0
    for r in rows:
        eid = int(r["id"])
        name = r["name"] or str(eid)
        done = int(r["indexed_count"] or 0)
        target = int(r["index_target"] or r["object_count"] or 0)
        log.warning(
            "resume interrupted indexing entity=%s name=%s indexed=%s/%s",
            eid,
            name,
            done,
            target,
        )
        usage_stats.record_lifecycle(
            "resume_index",
            f"{name} {done}/{target}",
        )
        jobs.submit(reindex_entity, eid, resume=True)
        n += 1
    return n


def reindex_entity(
    entity_id: int,
    *,
    scope: str | None = None,
    resume: bool = False,
    reset_bsl_methods: bool = False,
) -> None:
    lock = _try_acquire_reindex_lock(entity_id)
    if lock is None:
        log.warning("reindex skipped, already running in-process entity=%s", entity_id)
        return
    conn = None
    try:
        conn = connect(settings.db_path)
        client = EmbeddingClient()
        reindex_ok = False
        timer = _PhaseTimer()
        timer.mark("prep")
        setup_sec = embed_sec = 0.0
        entity = ent_repo.get_entity(conn, entity_id)
        if not entity:
            return
        if entity["status"] == "indexing" and not resume:
            return
        if entity["object_count"] <= 0:
            ent_repo.set_status(conn, entity_id, "index_error", error_message="Parse first")
            conn.commit()
            return

        if reset_bsl_methods and entity.get("bsl_enabled") and int(entity.get("bsl_method_count") or 0) > 0:
            t_reset = time.perf_counter()
            n = obj_repo.reset_method_embed_flags(conn, entity_id)
            conn.commit()
            log.info(
                "reindex queued BSL re-embed entity=%s methods=%s reset_sec=%.3f",
                entity_id,
                n,
                time.perf_counter() - t_reset,
            )

        model = entity["model"] or settings.default_embedding_model
        coll_path = collection_path(entity_id, model)
        zvec_store.release(coll_path)
        try:
            cleanup_zvec_crash_residue(coll_path)
        except Exception:
            log.debug("zvec residue cleanup before reindex failed entity=%s", entity_id, exc_info=True)
        pending_deletes = obj_repo.list_pending_zvec_deletes(conn, entity_id)
        pending_embeds = obj_repo.count_embed_pending(conn, entity_id)
        full_rebuild = not coll_path.exists()

        if full_rebuild:
            pending_embeds = int(entity["object_count"] or 0)

        index_scope = (
            _normalize_index_scope(scope)
            or str(entity.get("index_scope") or "")
            or _infer_index_scope(conn, entity_id, full_rebuild=full_rebuild)
        )
        if full_rebuild:
            done, target = 0, int(entity["object_count"] or 0)
        else:
            done, target = obj_repo.count_embed_progress(
                conn, entity_id, scope=index_scope
            )
        work = bool(pending_embeds or pending_deletes or full_rebuild)
        started = float(entity.get("index_started_at") or 0) if resume else 0.0
        if started <= 0:
            started = time.time()
        ent_repo.set_status(
            conn,
            entity_id,
            "indexing",
            error_message="",
            indexed_count=done,
            index_target=max(target, 1) if work else 0,
            index_started_at=started,
            index_scope=index_scope,
        )
        conn.commit()

        settings.zvec_dir.mkdir(parents=True, exist_ok=True)
        indexed = 0
        mode = "full" if full_rebuild else "incr"
        upsert_sec = stale_delete_sec = wave_flush_sec = zvec_flush_sec = 0.0

        timer.finish()  # close prep before detailed index phases
        if full_rebuild:
            dims = client.dims(model)
            (
                indexed,
                setup_sec,
                embed_sec,
                upsert_sec,
                stale_delete_sec,
                wave_flush_sec,
                zvec_flush_sec,
            ) = _reindex_full(
                conn, client, entity_id, model, coll_path, dims
            )
        else:
            (
                indexed,
                setup_sec,
                embed_sec,
                upsert_sec,
                stale_delete_sec,
                wave_flush_sec,
                zvec_flush_sec,
            ) = _reindex_incremental(
                conn, client, entity_id, model, coll_path, pending_deletes
            )
        timer.add("setup", setup_sec)
        timer.add("embed", embed_sec)
        timer.add("upsert", upsert_sec)
        timer.add("stale_delete", stale_delete_sec)
        timer.add("zvec_wave_flush", wave_flush_sec)
        timer.add("zvec_flush", zvec_flush_sec)

        timer.mark("finalize")
        total = int(entity["object_count"] or 0)
        pending = obj_repo.count_embed_pending(conn, entity_id)
        done_count = total if pending == 0 else max(0, total - pending)
        # Keep soft encoding warnings from parse; clear other leftover messages.
        prev_err = str(entity.get("error_message") or "")
        keep_err = prev_err if prev_err.startswith("encoding=") else ""
        ent_repo.set_status(
            conn,
            entity_id,
            "ready",
            indexed_count=done_count,
            index_target=total,
            index_started_at=0.0,
            index_scope="",
            error_message=keep_err,
        )
        conn.commit()
        log.info(
            "reindex done entity=%s mode=%s upserted=%s deletes=%s",
            entity_id,
            mode,
            indexed,
            len(pending_deletes) if mode == "incr" else 0,
        )
        timing = timer.summary()
        timer.log("reindex", entity_id, summary=timing, mode=mode, upserted=indexed)
        usage_stats.record(
            kind="index",
            name="reindex",
            ok=True,
            duration_ms=timer.snapshot()["duration_ms"],
            detail=f"mode={mode} upserted={indexed} {timing}",
            tier="usage",
            persist=False,
        )
        _persist_index_job(
            name="reindex",
            entity=entity,
            timer=timer,
            scale={
                "mode": mode,
                "upserted": indexed,
                "objects": int(entity.get("object_count") or 0),
                "methods": int(entity.get("bsl_method_count") or 0),
            },
        )
        reindex_ok = True
    except Exception as exc:
        reindex_ok = False
        log.exception("reindex failed entity=%s", entity_id)
        msg = str(exc)
        if conn is not None:
            ent_repo.set_status(
                conn,
                entity_id,
                "index_error",
                error_message=msg[:500],
                index_started_at=0.0,
            )
            conn.commit()
        usage_stats.record(
            kind="index", name="reindex", ok=False, detail=msg[:200], tier="usage", persist=False
        )
        usage_stats.record_error_detail(
            kind="index",
            name="reindex",
            context=str(entity_id),
            detail=msg[:2000],
        )
        try:
            if conn is not None:
                entity = ent_repo.get_entity(conn, entity_id)
                model = (entity or {}).get("model") or settings.default_embedding_model
                broken = collection_path(entity_id, model)
                # Always drop incomplete temp copy; keep final collection on LM outage
                # so embed_done + zvec resume still work after LM is back.
                _destroy_path(Path(str(broken) + ".__tmp__"))
                # Keep the live collection: resume continues from SQLite pending flags.
        except Exception:
            pass
    finally:
        if conn is not None:
            conn.close()
        lock.release()
