"""Снимок файлов dump/report для skip parse по mtime+size (path-режим).

Zip-upload сносит каталог — все mtime новые, skip не срабатывает. В path /
точке монтирования 1С меняет даты только у выгруженных заново файлов.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.services.path_nfc import nfc, nfc_rel

REPORT_FILE_REL = "__report__.txt"


def file_stamp(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) or None if the file cannot be stated."""
    try:
        st = path.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def rel_of(path: Path, root: Path) -> str:
    return nfc_rel(path.relative_to(root).as_posix())


def path_from_rel(root: Path, rel: str) -> Path:
    return root.joinpath(*PurePosixPath(rel.replace("\\", "/")).parts)


def tracked_files_unchanged(dumps_dir: Path, prev: dict[str, tuple[int, int]]) -> bool:
    """True if every snapshot file still exists with the same mtime+size.

    New files not in the snapshot are not detected (1С updates ConfigDumpInfo
    when the dump changes). Missing/changed tracked files return False.
    """
    if not prev:
        return False
    for rel, stamp in prev.items():
        cur = file_stamp(path_from_rel(dumps_dir, rel))
        if cur != stamp:
            return False
    return True


def drop_stamps_missing_meta_roots(
    conn: sqlite3.Connection,
    entity_id: int,
    dumps_dir: Path,
    stamps: dict[str, tuple[int, int]],
) -> int:
    """Remove stamps for ``Container/Name.xml`` when root object path is absent.

    Lets incremental parse re-read meta XML that previously yielded nothing
    (e.g. Report+SettingsStorage early-break) while children/assets already exist.
    Mutates ``stamps`` in place; returns number of removed entries.
    """
    from app.services.kinds import CONTAINER_EN_TO_RU

    if not stamps:
        return 0
    # rel → expected RU object path for top-level container XML only
    candidates: list[tuple[str, str]] = []
    for container_en, ru_plural in CONTAINER_EN_TO_RU.items():
        prefix = f"{container_en}/"
        for rel in list(stamps):
            if not rel.startswith(prefix) or rel.count("/") != 1:
                continue
            if not rel.lower().endswith(".xml"):
                continue
            name = nfc(Path(rel).stem)
            if not name:
                continue
            candidates.append((rel, f"{ru_plural}.{name}"))
    if not candidates:
        return 0
    paths = [p for _rel, p in candidates]
    existing: set[str] = set()
    chunk = 400
    for i in range(0, len(paths), chunk):
        part = paths[i : i + chunk]
        placeholders = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT path FROM objects
            WHERE entity_id=? AND path IN ({placeholders})
            """,
            (entity_id, *part),
        ).fetchall()
        for r in rows:
            existing.add(str(r["path"]))
    removed = 0
    for rel, path in candidates:
        if path in existing:
            continue
        if rel in stamps:
            del stamps[rel]
            removed += 1
    return removed


@dataclass
class DumpSidecars:
    forms: list[Path] = field(default_factory=list)
    templates: list[Path] = field(default_factory=list)
    help_html: list[Path] = field(default_factory=list)
    modules: list[Path] = field(default_factory=list)


def collect_sidecars(dumps_dir: Path, profile: dict) -> DumpSidecars:
    """One os.walk for Form.xml / Template.xml / Help html / BSL / Form.bin."""
    from app.services.dump_assets_parser import (
        is_managed_form_xml_rel,
        is_template_ext_xml_rel,
    )
    from app.services.help_parser import is_help_html_rel

    def _flag(key: str, default: bool = False) -> bool:
        v = profile.get(key)
        return v if isinstance(v, bool) else default

    want_forms = _flag("managed_forms")
    want_tpl = _flag("dcs") or _flag("spreadsheet_templates")
    want_help = _flag("help", True)
    want_bsl = _flag("bsl", True)
    want_bin = _flag("ordinary_forms")
    out = DumpSidecars()
    if not (want_forms or want_tpl or want_help or want_bsl or want_bin):
        return out

    skip_dirs = {"__macosx", ".git", ".svn", ".hg"}
    for dirpath, dirnames, filenames in os.walk(dumps_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        base = Path(dirpath)
        for name in filenames:
            lower = name.lower()
            path = base / name
            try:
                rel = PurePosixPath(nfc_rel(path.relative_to(dumps_dir).as_posix()))
            except ValueError:
                continue
            if want_forms and lower == "form.xml" and is_managed_form_xml_rel(rel):
                out.forms.append(path)
            elif want_tpl and lower == "template.xml" and is_template_ext_xml_rel(rel):
                out.templates.append(path)
            elif want_help and lower.endswith((".html", ".htm")) and is_help_html_rel(rel):
                out.help_html.append(path)
            elif want_bsl and lower.endswith(".bsl"):
                out.modules.append(path)
            elif (
                want_bin
                and lower == "form.bin"
                and rel.as_posix().lower().endswith("/ext/form.bin")
            ):
                out.modules.append(path)
    out.forms.sort()
    out.templates.sort()
    out.help_html.sort()
    out.modules.sort()
    return out


class DumpFileTracker:
    """Observe files during a dump walk: record stamps, decide parse vs skip."""

    def __init__(self, dumps_dir: Path, prev: dict[str, tuple[int, int]]):
        self.dumps_dir = dumps_dir
        self.prev = prev
        self.seen: dict[str, tuple[int, int]] = {}
        self.skipped: set[str] = set()
        self.parsed: set[str] = set()

    def observe(self, path: Path) -> bool:
        """Record stamp. Return True if the file content must be parsed."""
        try:
            rel = rel_of(path, self.dumps_dir)
        except ValueError:
            return True
        stamp = file_stamp(path)
        if stamp is None:
            return True
        self.seen[rel] = stamp
        old = self.prev.get(rel)
        if old is not None and old == stamp:
            self.skipped.add(rel)
            return False
        self.parsed.add(rel)
        return True
