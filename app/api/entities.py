from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.database import connect
from app.repositories import entities as ent_repo
from app.repositories import objects as obj_repo
from app.schemas.entities import (
    DEFAULT_INGEST_PROFILE,
    EntityCopy,
    EntityOut,
    EntityPatch,
    ImportPathRequest,
    ReindexResponse,
)
from app.services import jobs
from app.services.pipeline import (
    collection_path,
    delete_entity_upload_files,
    entity_dump_audit_path,
    entity_dump_dir,
    entity_report_path,
    entity_zip_path,
    iter_clone_entity,
    parse_entity,
    reindex_entity,
)
from app.services.parser import find_configuration_report_in_dir, peek_report_meta
from app.services.zvec_store import zvec_store
from app.services import runtime_settings
from app.services import mounts as mounts_svc

router = APIRouter(prefix="/api/entities", tags=["entities"])

log = logging.getLogger("cfsmcp2.entities")

_SAFE = re.compile(r"[^\w\-\.]+", re.UNICODE)


def _raise_if_name_taken(
    conn,
    tentative_name: str,
    *,
    report_name: str,
    report_synonym: str = "",
    report_version: str = "",
) -> None:
    """New ingest must not overwrite an existing context — UI asks for another name."""
    existing = ent_repo.get_entity_by_name(conn, tentative_name)
    if not existing:
        return
    taken = existing["name"]
    raise HTTPException(
        status_code=409,
        detail={
            "code": "name_conflict",
            "message": (
                f"Configuration '{report_name}' is already loaded as '{taken}'. "
                "Choose a different context name; the existing context will not be overwritten."
            ),
            "report_name": report_name,
            "report_synonym": report_synonym or "",
            "report_version": report_version or "",
            "existing_id": int(existing["id"]),
            "existing_name": taken,
            "suggested_name": ent_repo.suggest_unique_name(conn, tentative_name),
        },
    )


def _effective_bsl_load_mode(row: dict) -> str:
    """Empty mode means signatures (legacy / default). Only expose when BSL is loaded."""
    mode = str(row.get("bsl_load_mode") or "").strip().lower()
    if mode in {"signatures", "code", "full"}:
        return mode
    if int(row.get("bsl_method_count") or 0) > 0:
        return "signatures"
    return ""


def _effective_bsl_embed_mode(row: dict) -> str:
    """Empty embed mode means meta. Only expose when BSL is loaded."""
    from app.services.bsl_embed import ALLOWED_BSL_EMBED_MODES

    mode = str(row.get("bsl_embed_mode") or "").strip().lower()
    if mode in ALLOWED_BSL_EMBED_MODES:
        return mode
    if int(row.get("bsl_method_count") or 0) > 0:
        return "meta"
    return ""


def _row_to_out(row: dict) -> EntityOut:
    return EntityOut(
        id=row["id"],
        name=row["name"],
        synonym=row.get("synonym") or "",
        comment=row.get("comment") or "",
        entity_type=row.get("entity_type") or "configuration",
        version=row.get("version") or "",
        file_path=row.get("file_path") or "",
        source_mode=row.get("source_mode") or "report",
        source_location=row.get("source_location") or "upload",
        source_path=row.get("source_path") or "",
        ingest_profile=ent_repo.ingest_profile_of(row),
        dumps_dir=row.get("dumps_dir") or "",
        zip_path=row.get("zip_path") or "",
        enabled=bool(row["enabled"]),
        bsl_enabled=bool(row["bsl_enabled"]) if row.get("bsl_enabled") is not None else True,
        bsl_method_count=int(row.get("bsl_method_count") or 0),
        bsl_load_mode=_effective_bsl_load_mode(row),
        bsl_embed_mode=_effective_bsl_embed_mode(row),
        tag_ids=[int(t) for t in (row.get("tag_ids") or [])],
        model=row["model"],
        status=row["status"],
        name_locked=bool(row.get("name_locked")),
        object_count=row.get("object_count") or 0,
        link_count=row.get("link_count") or 0,
        indexed_count=row.get("indexed_count") or 0,
        index_target=row.get("index_target") or 0,
        index_started_at=float(row.get("index_started_at") or 0),
        index_scope=str(row.get("index_scope") or ""),
        parse_added=row.get("parse_added") or 0,
        parse_changed=row.get("parse_changed") or 0,
        parse_deleted=row.get("parse_deleted") or 0,
        parse_unchanged=row.get("parse_unchanged") or 0,
        error_message=row.get("error_message") or "",
        usage_calls=int(row.get("usage_calls") or 0),
        usage_share_pct=int(row.get("usage_share_pct") or 0),
        usage_bar_pct=int(row.get("usage_bar_pct") or 0),
        usage_window_sec=int(row.get("usage_window_sec") or 1200),
    )


def _attach_usage(rows: list[dict]) -> None:
    """Fill usage_* fields from in-memory MCP stats (configured window)."""
    from app.services.usage_stats import usage_stats

    snap = usage_stats.mcp_usage_by_context()
    by_name = snap.get("by_name") or {}
    total = int(snap.get("total") or 0)
    max_calls = int(snap.get("max_calls") or 0)
    window_sec = int(snap.get("window_sec") or 1200)
    for r in rows:
        name = str(r.get("name") or "")
        calls = int(by_name.get(name) or 0)
        share = int(round(100.0 * calls / total)) if total > 0 and calls > 0 else 0
        bar = int(round(100.0 * calls / max_calls)) if max_calls > 0 and calls > 0 else 0
        r["usage_calls"] = calls
        r["usage_share_pct"] = min(100, share)
        r["usage_bar_pct"] = min(100, bar)
        r["usage_window_sec"] = window_sec


def _merged_ingest_profile(raw: str | None) -> dict:
    """Merge user profile JSON onto DEFAULT_INGEST_PROFILE (ТЗ v0.4 §4)."""
    profile = dict(DEFAULT_INGEST_PROFILE)
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "ingest_profile must be a valid JSON object") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "ingest_profile must be a JSON object")
        profile.update(data)
    return profile


def _path_is_inside(path: Path, root: Path) -> bool:
    """Resolve both sides and check containment (Windows pathlib is case-insensitive)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _assert_source_path_allowed(src: Path, raw_path: str) -> None:
    """MOUNT_POINTS / PATH_ALLOWED_ROOTS — как у import-path."""
    roots, root_src = mounts_svc.allowed_roots_for_import()
    if root_src == "mounts":
        if not roots:
            raise HTTPException(
                400,
                "MOUNT_POINTS заданы, но ни одна точка недоступна (проверьте volumes в compose)",
            )
        if not any(mounts_svc.path_is_inside(src, r) for r in roots):
            ids = ", ".join(m.id for m in mounts_svc.parse_mount_points()) or "(none)"
            raise HTTPException(
                400,
                f"source_path вне точек подключения ({ids}): {raw_path}",
            )
    elif roots:
        if not any(_path_is_inside(src, Path(r)) for r in roots):
            raise HTTPException(
                400,
                f"source_path вне разрешённых корней (outside allowed roots): {raw_path}",
            )
    else:
        log.warning(
            "source_path: MOUNT_POINTS и PATH_ALLOWED_ROOTS пусты — корни не ограничены, путь: %s",
            raw_path,
        )


def _resolve_external_source_path(raw_path: str, source_mode: str) -> Path:
    """Проверить абсолютный путь под source_mode сущности (без авто-детекта режима)."""
    mode = (source_mode or "report").strip().lower()
    if mode not in ("report", "dump"):
        raise HTTPException(400, f"unsupported source_mode: {source_mode!r}")
    path = (raw_path or "").strip()
    if not path:
        raise HTTPException(400, "source_path is required")
    if not os.path.isabs(path):
        raise HTTPException(400, f"source_path должен быть абсолютным путём: {path}")
    src = Path(path)
    if not src.exists():
        raise HTTPException(400, f"source_path не существует: {path}")
    _assert_source_path_allowed(src, path)

    if src.is_dir():
        report_in_dir = find_configuration_report_in_dir(src)
        has_cfg = (src / "Configuration.xml").is_file()
        if mode == "dump":
            if not has_cfg:
                raise HTTPException(
                    400,
                    f"каталог не похож на dump: нет Configuration.xml в {path}",
                )
            return src.resolve()
        if report_in_dir is None:
            raise HTTPException(
                400,
                f"source_mode=report: в каталоге нет файла отчёта .txt "
                f"(например ОтчетПоКонфигурации.txt): {path}",
            )
        return report_in_dir.resolve()
    if src.is_file():
        if mode == "dump":
            raise HTTPException(
                400,
                "source_mode=dump требует каталог с Configuration.xml, а не файл",
            )
        return src.resolve()
    raise HTTPException(400, f"source_path не является файлом или каталогом: {path}")


def _looks_like_zip(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _remove_server_dump_artifacts(entity: dict | None) -> None:
    """Удаляет серверные dump-артефакты (dumps/e{id}/, zip, audit) для upload-режима.

    Для source_location=path ничего не трогаем (внешний каталог пользователя).
    """
    if not entity:
        return
    if (entity.get("source_location") or "").strip() != "upload":
        return
    eid = int(entity["id"])
    for p in (entity_dump_dir(eid), entity_zip_path(eid), entity_dump_audit_path(eid)):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        except OSError:
            log.debug("failed removing old dump artifact %s", p, exc_info=True)


@router.get("")
def list_entities() -> list[EntityOut]:
    conn = connect(settings.db_path)
    try:
        from app.repositories import tags as tag_repo

        rows = ent_repo.list_entities(conn)
        by_tags = tag_repo.tags_by_entity_ids(conn, [int(r["id"]) for r in rows])
        for r in rows:
            r["tag_ids"] = by_tags.get(int(r["id"]), [])
        _attach_usage(rows)
        return [_row_to_out(r) for r in rows]
    finally:
        conn.close()


@router.post("/upload")
async def upload_entity(
    file: Annotated[UploadFile, File()],
    model: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
    comment: Annotated[str | None, Form()] = None,
    merge: Annotated[str | None, Form()] = None,
    entity_id: Annotated[str | None, Form()] = None,
    ingest_profile: Annotated[str | None, Form()] = None,
) -> EntityOut:
    """Upload a report (txt). Without Name override uses report ``Имя``.
    If that name already exists, returns 409 ``name_conflict`` — UI asks for another name
    (existing context is never overwritten). Pass ``entity_id`` to refresh that row.

    cfsmcp2 (§4/§6): ``ingest_profile`` (JSON) мержится с DEFAULT_INGEST_PROFILE и
    сохраняется в ``entities.ingest_profile`` (только для новой сущности; на
    «Обновить» профиль не меняется). ``source_mode='report'``,
    ``source_location='upload'``, ``source_path`` = имя загруженного файла.
    """
    settings.metadata_dir.mkdir(parents=True, exist_ok=True)
    original = file.filename or "report.txt"
    safe = _SAFE.sub("_", original).strip("._") or "report.txt"
    name_override = (name or "").strip()
    name_locked = bool(name_override)
    comment_override = (comment or "").strip() or None
    merge_flag = str(merge or "").strip().lower() in {"1", "true", "yes", "on"}
    target_id: int | None = None
    if entity_id is not None and str(entity_id).strip() != "":
        try:
            target_id = int(str(entity_id).strip())
        except ValueError as exc:
            raise HTTPException(400, "Invalid entity_id") from exc
    use_model = model or runtime_settings.get_default_embedding_model()
    file_stem = Path(safe).stem
    profile = _merged_ingest_profile(ingest_profile)

    tmp = settings.metadata_dir / f".upload_{uuid.uuid4().hex}.part"
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        meta = peek_report_meta(tmp, file_stem)
        report_name = (meta.config_name or "").strip() or file_stem
        tentative_name = name_override if name_locked else report_name

        conn = connect(settings.db_path)
        try:
            if target_id is not None:
                existing = ent_repo.get_entity(conn, target_id)
                if not existing:
                    raise HTTPException(404, "Entity not found")
                use_model = model or existing.get("model") or use_model
                old_path = Path(existing["file_path"]) if existing.get("file_path") else None
                entity_id_out = ent_repo.refresh_entity_file(
                    conn,
                    target_id,
                    synonym=meta.config_synonym or existing.get("synonym") or "",
                    version=meta.version or existing.get("version") or "",
                    file_path=str(tmp.resolve()),
                    model=use_model,
                    entity_type=meta.entity_type or "configuration",
                    source_path=original,
                    comment=comment_override,
                )
            else:
                _raise_if_name_taken(
                    conn,
                    tentative_name,
                    report_name=report_name,
                    report_synonym=meta.config_synonym or "",
                    report_version=meta.version or "",
                )
                old_path = None

                entity_id_out = ent_repo.upsert_entity(
                    conn,
                    name=tentative_name,
                    synonym=meta.config_synonym or "",
                    version=meta.version or "",
                    file_path=str(tmp.resolve()),
                    model=use_model,
                    entity_type=meta.entity_type or "configuration",
                    name_locked=name_locked,
                    source_mode="report",
                    source_location="upload",
                    source_path=original,
                    ingest_profile=profile,
                    comment=comment_override,
                )

            dest = entity_report_path(entity_id_out)

            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            tmp.replace(dest)
            tmp = None  # moved

            conn.execute(
                "UPDATE entities SET file_path=?, updated_at=datetime('now') WHERE id=?",
                (str(dest.resolve()), entity_id_out),
            )
            conn.commit()
            row = ent_repo.get_entity(conn, entity_id_out)

            if old_path is not None:
                try:
                    meta_root = settings.metadata_dir.resolve()
                    op = old_path.resolve()
                    if (
                        op != dest.resolve()
                        and op.exists()
                        and op.parent == meta_root
                        and not op.name.startswith(".upload_")
                    ):
                        op.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            conn.close()
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)

    jobs.submit(parse_entity, entity_id_out)
    return _row_to_out(row)


@router.post("/upload-dump")
async def upload_dump_entity(
    file: Annotated[UploadFile, File()],
    model: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
    comment: Annotated[str | None, Form()] = None,
    merge: Annotated[str | None, Form()] = None,
    entity_id: Annotated[str | None, Form()] = None,
    ingest_profile: Annotated[str | None, Form()] = None,
    source_path: Annotated[str | None, Form()] = None,
    source_mode: Annotated[str | None, Form()] = None,
) -> EntityOut:
    """Upload a dump (zip, Hierarchical-выгрузка 1С) — этап 3, ТЗ §15 п.3.

    Zip сохраняется целиком как ``dumps/e{id}.zip``; распаковываются ТОЛЬКО
    файлы allowlist профиля (§5.3) в ``dumps/e{id}/`` (безопасно: защита от
    path traversal и zip bomb). ``source_mode='dump'``, ``source_location='upload'``,
    ``file_path``/``zip_path`` = путь zip, ``dumps_dir`` = каталог распаковки.

    Pass ``entity_id`` to replace the dump of that row (refresh; остаётся upload).
    """
    from app.services.dump_parser import read_configuration_meta_from_zip
    from app.services.dump_zip import ZipSafetyError, extract_dump_zip

    settings.dumps_dir.mkdir(parents=True, exist_ok=True)
    original = file.filename or "dump.zip"
    safe = _SAFE.sub("_", original).strip("._") or "dump.zip"
    name_override = (name or "").strip()
    name_locked = bool(name_override)
    comment_override = (comment or "").strip() or None
    merge_flag = str(merge or "").strip().lower() in {"1", "true", "yes", "on"}
    target_id: int | None = None
    if entity_id is not None and str(entity_id).strip() != "":
        try:
            target_id = int(str(entity_id).strip())
        except ValueError as exc:
            raise HTTPException(400, "Invalid entity_id") from exc
    use_model = model or runtime_settings.get_default_embedding_model()
    profile = _merged_ingest_profile(ingest_profile)

    tmp = settings.dumps_dir / f".upload_{uuid.uuid4().hex}.zip.part"
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        if not _looks_like_zip(tmp):
            raise HTTPException(400, "upload-dump requires a ZIP archive (dump of configuration)")

        meta = read_configuration_meta_from_zip(tmp)
        dump_name = (meta.config_name or "").strip() or Path(safe).stem
        tentative_name = name_override if name_locked else dump_name

        conn = connect(settings.db_path)
        try:
            if target_id is not None:
                existing = ent_repo.get_entity(conn, target_id)
                if not existing:
                    raise HTTPException(404, "Entity not found")
                use_model = model or existing.get("model") or use_model
                _remove_server_dump_artifacts(existing)
                entity_id_out = ent_repo.refresh_entity_file(
                    conn,
                    target_id,
                    synonym=meta.config_synonym or existing.get("synonym") or "",
                    version=meta.version or existing.get("version") or "",
                    file_path="",
                    model=use_model,
                    entity_type=meta.entity_type or "configuration",
                    source_path=original,
                    source_location="upload",
                    dumps_dir="",
                    zip_path="",
                    comment=comment_override,
                )
            else:
                _raise_if_name_taken(
                    conn,
                    tentative_name,
                    report_name=dump_name,
                    report_synonym=meta.config_synonym or "",
                    report_version=meta.version or "",
                )
                entity_id_out = ent_repo.upsert_entity(
                    conn,
                    name=tentative_name,
                    synonym=meta.config_synonym or "",
                    version=meta.version or "",
                    file_path="",
                    model=use_model,
                    entity_type=meta.entity_type or "configuration",
                    name_locked=name_locked,
                    source_mode="dump",
                    source_location="upload",
                    source_path=original,
                    ingest_profile=profile,
                    dumps_dir="",
                    zip_path="",
                    comment=comment_override,
                )
            conn.commit()
        finally:
            conn.close()

        zip_dest = entity_zip_path(entity_id_out)
        zip_dest.parent.mkdir(parents=True, exist_ok=True)
        if zip_dest.exists():
            zip_dest.unlink()
        tmp.replace(zip_dest)
        tmp = None  # moved

        dumps_dir_dest = entity_dump_dir(entity_id_out)
        shutil.rmtree(dumps_dir_dest, ignore_errors=True)
        try:
            stats = extract_dump_zip(zip_dest, dumps_dir_dest, profile)
        except ZipSafetyError as exc:
            raise HTTPException(400, f"zip rejected: {exc}") from exc

        from app.services.dump_zip import resolve_dump_root

        dumps_root = resolve_dump_root(dumps_dir_dest)

        conn = connect(settings.db_path)
        try:
            conn.execute(
                "UPDATE entities SET file_path=?, dumps_dir=?, zip_path=? WHERE id=?",
                (
                    str(zip_dest.resolve()),
                    str(dumps_root.resolve()),
                    str(zip_dest.resolve()),
                    entity_id_out,
                ),
            )
            conn.commit()
            row = ent_repo.get_entity(conn, entity_id_out)
        finally:
            conn.close()

        log.info(
            "dump upload entity=%s zip=%s extracted=%s skipped=%s rejected=%s",
            entity_id_out,
            zip_dest.name,
            stats.extracted,
            stats.skipped,
            stats.rejected,
        )
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)

    jobs.submit(parse_entity, entity_id_out)
    return _row_to_out(row)


@router.post("/import-path")
def import_path_entity(body: ImportPathRequest) -> EntityOut:
    """Import metadata from a server-visible path (no upload; source_location=path).

    Этап 2.5/3 (ТЗ §11): ``source_mode`` — ``report`` (файл txt или каталог с
    ``ОтчетПоКонфигурации*.txt`` в корне) или ``dump`` (каталог Hierarchical-выгрузки
    с ``Configuration.xml``). При пустом ``source_mode`` — детект: каталог с
    ``Configuration.xml`` → dump, иначе txt-отчёт в корне каталога / файл → report.

    Файл/каталог источника НЕ копируется в ``metadata_dir`` — ``file_path``/
    ``source_path``/``dumps_dir`` указывают на внешний путь, при delete
    пользовательские файлы не удаляются. Путь должен быть абсолютным и
    существовать. Если ``PATH_ALLOWED_ROOTS`` непуст — путь обязан лежать внутри
    одного из корней; пустой список = без ограничений (warning).

    ``entity_id`` — обновление существующей сущности (refresh). ``source_location``
    становится ``'path'``; если сущность была upload — старые серверные
    dump-артефакты удаляются. ``merge``/``name`` разрешают конфликт имени.
    """
    source_mode = (body.source_mode or "").strip().lower()
    if source_mode not in ("", "auto", "report", "dump"):
        raise HTTPException(400, f"unsupported source_mode: {body.source_mode!r}")

    raw_path = (body.source_path or "").strip()
    if not raw_path:
        raise HTTPException(400, "source_path is required")
    if not os.path.isabs(raw_path):
        raise HTTPException(400, f"source_path должен быть абсолютным путём: {raw_path}")
    src = Path(raw_path)
    if not src.exists():
        raise HTTPException(400, f"source_path не существует: {raw_path}")
    _assert_source_path_allowed(src, raw_path)

    if src.is_dir():
        # Каталог: dump (Configuration.xml) или report (.txt отчёта в корне).
        report_in_dir = find_configuration_report_in_dir(src)
        has_cfg = (src / "Configuration.xml").is_file()
        if source_mode == "report":
            if report_in_dir is None:
                raise HTTPException(
                    400,
                    f"source_mode=report: в каталоге нет файла отчёта .txt "
                    f"(например ОтчетПоКонфигурации.txt): {raw_path}",
                )
            src = report_in_dir
            mode = "report"
        elif source_mode == "dump":
            if not has_cfg:
                raise HTTPException(
                    400,
                    f"каталог не похож на dump: нет Configuration.xml в {raw_path}",
                )
            mode = "dump"
        else:
            # auto: dump приоритетнее, если есть Configuration.xml; иначе отчёт в корне
            if has_cfg:
                mode = "dump"
            elif report_in_dir is not None:
                src = report_in_dir
                mode = "report"
            else:
                raise HTTPException(
                    400,
                    f"каталог не похож на dump (нет Configuration.xml) и нет файла отчёта .txt: {raw_path} "
                    "(import-path: файл-отчёт, каталог dump, либо каталог с ОтчетПоКонфигурации.txt)",
                )
    elif src.is_file():
        if source_mode == "dump":
            raise HTTPException(
                400,
                "source_mode=dump требует каталог с Configuration.xml, а не файл",
            )
        mode = "report"
    else:
        raise HTTPException(400, f"source_path не является файлом или каталогом: {raw_path}")

    resolved = src.resolve()
    name_override = (body.name or "").strip()
    name_locked = bool(name_override)
    comment_override = (body.comment or "").strip() or None
    merge_flag = bool(body.merge)
    target_id = body.entity_id
    use_model = (body.model or "").strip() or runtime_settings.get_default_embedding_model()
    profile = _merged_ingest_profile(body.ingest_profile)

    if mode == "dump":
        from app.services.dump_parser import read_configuration_meta

        meta = read_configuration_meta(resolved)
        report_name = (meta.config_name or "").strip() or resolved.name
    else:
        meta = peek_report_meta(resolved, resolved.stem)
        report_name = (meta.config_name or "").strip() or resolved.stem
    tentative_name = name_override if name_locked else report_name

    conn = connect(settings.db_path)
    try:
        if target_id is not None:
            existing = ent_repo.get_entity(conn, target_id)
            if not existing:
                raise HTTPException(404, "Entity not found")
            use_model = existing.get("model") or use_model
            _remove_server_dump_artifacts(existing)
            entity_id_out = ent_repo.refresh_entity_file(
                conn,
                target_id,
                synonym=meta.config_synonym or existing.get("synonym") or "",
                version=meta.version or existing.get("version") or "",
                file_path=str(resolved),
                model=use_model,
                entity_type=meta.entity_type or "configuration",
                source_path=str(resolved),
                source_location="path",
                source_mode=mode,
                dumps_dir=str(resolved) if mode == "dump" else None,
                zip_path="" if mode == "dump" else None,
                comment=comment_override,
            )
        else:
            _raise_if_name_taken(
                conn,
                tentative_name,
                report_name=report_name,
                report_synonym=meta.config_synonym or "",
                report_version=meta.version or "",
            )
            entity_id_out = ent_repo.upsert_entity(
                conn,
                name=tentative_name,
                synonym=meta.config_synonym or "",
                version=meta.version or "",
                file_path=str(resolved),
                model=use_model,
                entity_type=meta.entity_type or "configuration",
                name_locked=name_locked,
                source_mode=mode,
                source_location="path",
                source_path=str(resolved),
                ingest_profile=profile,
                dumps_dir=str(resolved) if mode == "dump" else "",
                zip_path="",
                comment=comment_override,
            )
        conn.commit()
        row = ent_repo.get_entity(conn, entity_id_out)
    finally:
        conn.close()

    if row is None:
        raise HTTPException(404, "Entity not found after import-path")

    jobs.submit(parse_entity, entity_id_out)
    return _row_to_out(row)


@router.post("/{entity_id}/copy")
def copy_entity(entity_id: int, body: EntityCopy) -> StreamingResponse:
    """Copy a ready entity under a new name; stream NDJSON progress for the UI."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        if row.get("status") != "ready":
            raise HTTPException(409, "Only ready (fully indexed) entities can be copied")
        if ent_repo.get_entity_by_name(conn, name):
            raise HTTPException(409, f"Name already exists: {name}")
    finally:
        conn.close()

    def _emit(pct: int, step: str, detail: str = "", **extra) -> str:
        payload = {"pct": pct, "step": step, "detail": detail, **extra}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    def generate() -> Iterator[str]:
        try:
            for ev in iter_clone_entity(entity_id, name):
                step = str(ev.get("step") or "")
                pct = int(ev.get("pct") or 0)
                detail = str(ev.get("detail") or "")
                if step == "done":
                    new_id = int(ev["new_id"])
                    if ev.get("needs_reindex"):
                        jobs.submit(reindex_entity, new_id)
                    yield _emit(pct, step, detail, new_id=new_id)
                else:
                    yield _emit(pct, step, detail)
        except ValueError as exc:
            yield _emit(0, "error", str(exc)[:300])
        except Exception as exc:
            yield _emit(0, "error", ("Copy failed: " + str(exc))[:300])

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.patch("/{entity_id}")
def patch_entity(entity_id: int, body: EntityPatch) -> EntityOut:
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        # Один выключатель на строку: enabled и bsl_enabled всегда синхронны.
        # Отдельный bsl_enabled в PATCH (совместимость) трактуется как тот же toggle.
        toggle = body.enabled if body.enabled is not None else body.bsl_enabled
        if toggle is not None:
            flag = 1 if toggle else 0
            ent_repo.set_status(
                conn, entity_id, row["status"], enabled=flag, bsl_enabled=flag
            )
        if body.tag_ids is not None:
            from app.repositories import tags as tag_repo

            for tid in body.tag_ids:
                if not tag_repo.get_tag(conn, tid):
                    raise HTTPException(404, f"Tag not found: {tid}")
            tag_repo.set_entity_tags(conn, entity_id, body.tag_ids)
        if body.model is not None and body.model != row["model"]:
            from app.repositories import objects as obj_repo

            # model change requires full reindex into a new collection
            obj_repo.reset_embed_flags(conn, entity_id)
            obj_repo.clear_pending_zvec_deletes(conn, entity_id)
            ent_repo.set_status(
                conn, entity_id, "parsed", model=body.model, indexed_count=0, index_target=0
            )
        if body.comment is not None:
            ent_repo.set_comment(conn, entity_id, body.comment)
        if body.name is not None:
            try:
                ent_repo.rename_entity(conn, entity_id, body.name)
            except KeyError as exc:
                raise HTTPException(404, "Entity not found") from exc
            except ValueError as exc:
                msg = str(exc)
                code = 409 if "busy" in msg.lower() or "already used" in msg.lower() else 400
                raise HTTPException(code, msg) from exc
        if body.source_path is not None:
            # Без ingest: смена пути / точки подключения. Path → file_path/dumps_dir;
            # upload → только закладка source_path.
            if row["status"] in ("parsing", "indexing", "uploaded", "loading_modules"):
                raise HTTPException(409, "Entity is busy; cannot change source_path")
            raw = (body.source_path or "").strip()
            loc = (row.get("source_location") or "upload").strip().lower()
            mode = (row.get("source_mode") or "report").strip().lower()
            if not raw:
                if loc == "path":
                    raise HTTPException(
                        400,
                        "source_path нельзя очистить в path-режиме (укажите абсолютный путь)",
                    )
                ent_repo.update_source_path(conn, entity_id, source_path="")
            else:
                resolved = _resolve_external_source_path(raw, mode)
                resolved_s = str(resolved)
                if loc == "path":
                    ent_repo.update_source_path(
                        conn,
                        entity_id,
                        source_path=resolved_s,
                        file_path=resolved_s,
                        dumps_dir=resolved_s if mode == "dump" else "",
                        zip_path="" if mode == "dump" else None,
                    )
                else:
                    ent_repo.update_source_path(
                        conn, entity_id, source_path=resolved_s
                    )
        conn.commit()
        row = ent_repo.get_entity(conn, entity_id)
        from app.repositories import tags as tag_repo

        if row:
            row = dict(row)
            row["tag_ids"] = tag_repo.list_tag_ids_for_entity(conn, entity_id)
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM objects WHERE entity_id=? AND kind IN ('Procedure','Function')",
                (entity_id,),
            ).fetchone()["c"]
            row["bsl_method_count"] = int(cnt)
        return _row_to_out(row)
    finally:
        conn.close()


@router.delete("/{entity_id}")
def delete_entity(entity_id: int) -> StreamingResponse:
    """Delete entity; stream NDJSON progress lines for the UI progress modal."""

    def _emit(pct: int, step: str, detail: str = "") -> str:
        return json.dumps(
            {"pct": pct, "step": step, "detail": detail},
            ensure_ascii=False,
        ) + "\n"

    def generate() -> Iterator[str]:
        conn = connect(settings.db_path)
        try:
            row = ent_repo.get_entity(conn, entity_id)
            if not row:
                yield _emit(0, "error", "Entity not found")
                return
            name = row["name"] or f"#{entity_id}"
            yield _emit(5, "prepare", name)

            coll = collection_path(row["id"], row["model"])
            yield _emit(15, "close_index", name)
            zvec_store.release(coll)

            if coll.exists():
                yield _emit(30, "destroy_index", name)
                try:
                    import zvec

                    zvec.open(str(coll)).destroy()
                except Exception:
                    shutil.rmtree(coll, ignore_errors=True)
                yield _emit(65, "destroy_index", name)
            else:
                yield _emit(65, "destroy_index", name)

            yield _emit(75, "delete_file", name)
            # Guard (этап 2.5): удаляем только файлы, которыми владеет сервер.
            # Для path-режима (source_location=path) file_path указывает на внешний
            # файл пользователя — он и каталог источника НЕ удаляются.
            extras: list[Path] = []
            if row.get("file_path") and row.get("source_location") == "upload":
                extras.append(Path(row["file_path"]))
            delete_entity_upload_files(entity_id, extra_paths=extras)

            yield _emit(90, "delete_db", name)
            ent_repo.delete_entity(conn, entity_id)
            conn.commit()
            yield _emit(100, "done", name)
        except Exception as exc:
            yield _emit(0, "error", str(exc)[:300])
        finally:
            conn.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/{entity_id}/reindex")
def start_reindex(entity_id: int) -> ReindexResponse:
    """Queue incremental BSL re-embed. Generation bump is one row — safe in this request.

    Worker must not bump again: if the process dies after this commit, resume
    continues from pending methods (``bsl_embed_gen`` already advanced).
    """
    methods = 0
    total = 0
    done = 0
    conn = connect(settings.db_path)
    try:
        row = ent_repo.get_entity(conn, entity_id)
        if not row:
            raise HTTPException(404, "Entity not found")
        methods = int(row.get("bsl_method_count") or 0) if row.get("bsl_enabled") else 0
        total = int(row.get("object_count") or 0)
        scope = "bsl" if methods else "report"
        pending_before = obj_repo.count_embed_pending(conn, entity_id)
        # index_error + leftover pending = resume the same generation (do not bump).
        resume_only = row["status"] == "index_error" and pending_before > 0
        bump = bool(methods and not resume_only)
        if bump:
            done, target = 0, max(methods, 1)
        else:
            done, target = obj_repo.count_embed_progress(conn, entity_id, scope=scope)
            target = max(target, 1)
        claimed = ent_repo.claim_indexing(
            conn,
            entity_id,
            indexed_count=done,
            index_target=target,
            index_started_at=time.time(),
            index_scope=scope,
            bump_bsl_gen=bump,
        )
        if not claimed:
            now = ent_repo.get_entity(conn, entity_id) or row
            st = str(now.get("status") or "")
            if st == "parsing":
                raise HTTPException(409, "Still parsing")
            if st == "indexing":
                raise HTTPException(409, "Already indexing")
            if int(now.get("object_count") or 0) <= 0:
                raise HTTPException(409, "Parse not finished")
            raise HTTPException(409, "Already indexing")
        conn.commit()
    finally:
        conn.close()
    jobs.submit(reindex_entity, entity_id, resume=True)
    detail = "Reindex started"
    if methods:
        detail = f"Reindex started ({methods} methods queued for re-embed)"
    return ReindexResponse(
        id=entity_id,
        status="indexing",
        detail=detail,
        indexed_count=done,
        object_count=total,
        progress_pct=0,
    )
