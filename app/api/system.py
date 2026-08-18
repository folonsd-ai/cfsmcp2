"""Системные UI-хелперы: runtime/mounts, browse, нативный выбор пути (localhost)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import mounts as mounts_svc

log = logging.getLogger("cfsmcp2.system")

router = APIRouter(prefix="/api/system", tags=["system"])

_pick_lock = asyncio.Lock()


class PickPathRequest(BaseModel):
    mode: str = Field("directory", description="file | directory")
    initial: str | None = None


class BrowseMountRequest(BaseModel):
    mount_id: str = Field(..., min_length=1)
    rel: str = ""


@router.get("/runtime")
def get_runtime():
    """Docker/native mode, mount points, whether native pick-path is available."""
    return mounts_svc.runtime_snapshot()


@router.get("/browse")
def browse_mount(
    mount_id: str = Query(..., min_length=1),
    rel: str = Query(""),
):
    """List dirs and .txt under a named mount (Docker path picker)."""
    try:
        return mounts_svc.browse_mount(mount_id, rel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/browse")
def browse_mount_post(body: BrowseMountRequest):
    try:
        return mounts_svc.browse_mount(body.mount_id, body.rel or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/suggest-path")
def suggest_path(source_path: str = Query(..., min_length=1)):
    """When an old path is missing, suggest mounts with the same folder name."""
    suggestions = mounts_svc.suggest_paths_for_missing(source_path)
    missing = True
    try:
        missing = not Path(source_path).exists()
    except OSError:
        missing = True
    return {
        "source_path": source_path,
        "missing": missing,
        "suggestions": suggestions,
    }


@router.get("/peek-source")
def peek_source(source_path: str = Query(..., min_length=1)):
    """Прочитать имя/синоним/версию из Configuration.xml или txt-отчёта (без ingest)."""
    import os

    from app.services.dump_parser import read_configuration_meta
    from app.services.dump_zip import resolve_dump_root
    from app.services.parser import find_configuration_report_in_dir, peek_report_meta

    raw = (source_path or "").strip()
    if not raw:
        raise HTTPException(400, "source_path is required")
    if not os.path.isabs(raw):
        raise HTTPException(400, f"source_path должен быть абсолютным путём: {raw}")
    src = Path(raw)
    if not src.exists():
        raise HTTPException(400, f"source_path не существует: {raw}")

    roots, root_src = mounts_svc.allowed_roots_for_import()
    if root_src == "mounts":
        if not roots:
            raise HTTPException(400, "MOUNT_POINTS заданы, но ни одна точка недоступна")
        if not any(mounts_svc.path_is_inside(src, r) for r in roots):
            raise HTTPException(400, f"source_path вне точек подключения: {raw}")
    elif roots:
        try:
            ok = any(src.resolve().is_relative_to(Path(r).resolve()) for r in roots)
        except OSError:
            ok = False
        if not ok:
            raise HTTPException(400, f"source_path вне разрешённых корней: {raw}")

    try:
        if src.is_dir():
            root = resolve_dump_root(src)
            if (root / "Configuration.xml").is_file():
                meta = read_configuration_meta(root)
                return {
                    "ok": True,
                    "source_mode": "dump",
                    "name": (meta.config_name or "").strip(),
                    "synonym": (meta.config_synonym or "").strip(),
                    "version": (meta.version or "").strip(),
                    "entity_type": meta.entity_type or "configuration",
                    "resolved_path": str(root),
                }
            report = find_configuration_report_in_dir(src)
            if report is not None:
                meta = peek_report_meta(report, report.stem)
                return {
                    "ok": True,
                    "source_mode": "report",
                    "name": (meta.config_name or "").strip(),
                    "synonym": (meta.config_synonym or "").strip(),
                    "version": (meta.version or "").strip(),
                    "entity_type": meta.entity_type or "configuration",
                    "resolved_path": str(report),
                }
            raise HTTPException(
                400,
                "каталог без Configuration.xml и без файла отчёта .txt",
            )
        if src.is_file():
            meta = peek_report_meta(src, src.stem)
            return {
                "ok": True,
                "source_mode": "report",
                "name": (meta.config_name or "").strip(),
                "synonym": (meta.config_synonym or "").strip(),
                "version": (meta.version or "").strip(),
                "entity_type": meta.entity_type or "configuration",
                "resolved_path": str(src.resolve()),
            }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("peek-source failed path=%s", raw)
        raise HTTPException(400, f"не удалось прочитать метаданные: {exc}") from exc
    raise HTTPException(400, f"source_path не является файлом или каталогом: {raw}")


def _client_is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _initial_dir(initial: str | None) -> str | None:
    if not initial:
        return None
    p = Path(initial)
    try:
        if p.is_file():
            return str(p.parent)
        if p.is_dir():
            return str(p)
        parent = p.parent
        if parent.is_dir():
            return str(parent)
    except OSError:
        return None
    return None


def _pick_path_sync(mode: str, initial: str | None) -> str | None:
    """Блокирующий нативный диалог (tkinter). Вызывать из thread."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.wm_attributes("-topmost", True)
    except tk.TclError:
        pass
    root.update_idletasks()
    start = _initial_dir(initial)
    try:
        if mode == "file":
            chosen = filedialog.askopenfilename(
                parent=root,
                title="Выберите файл отчёта (.txt)",
                initialdir=start or None,
                filetypes=[
                    ("Отчёт по конфигурации", "*.txt"),
                    ("Все файлы", "*.*"),
                ],
            )
        else:
            chosen = filedialog.askdirectory(
                parent=root,
                title="Выберите каталог полной выгрузки",
                initialdir=start or None,
                mustexist=True,
            )
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    path = str(chosen or "").strip()
    return path or None


@router.post("/pick-path")
async def pick_path(body: PickPathRequest, request: Request):
    """Открыть системный диалог выбора файла/каталога и вернуть абсолютный путь.

    Только с loopback: браузер не отдаёт абсолютные пути, а import-path их требует.
    В runtime=docker нативный диалог отключён — используйте browse по mount.
    """
    if not _client_is_loopback(request):
        raise HTTPException(403, "Native path picker is available only from localhost")
    snap = mounts_svc.runtime_snapshot()
    if not snap.get("pick_path_available"):
        raise HTTPException(
            400,
            "Native path picker is unavailable in Docker runtime; use mount browse instead",
        )
    mode = (body.mode or "directory").strip().lower()
    if mode not in ("file", "directory"):
        raise HTTPException(400, "mode must be 'file' or 'directory'")
    if _pick_lock.locked():
        raise HTTPException(409, "A path picker dialog is already open")
    async with _pick_lock:
        try:
            path = await asyncio.to_thread(_pick_path_sync, mode, body.initial)
        except Exception as exc:
            log.exception("pick-path failed")
            raise HTTPException(500, f"Path picker failed: {exc}") from exc
    return {"path": path or "", "cancelled": not bool(path), "mode": mode}
