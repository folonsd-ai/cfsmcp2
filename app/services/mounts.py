"""Named mount points (Docker) and runtime mode detection."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core.config import settings

log = logging.getLogger("cfsmcp2.mounts")

RuntimeMode = Literal["docker", "native"]
RuntimeModeSetting = Literal["auto", "docker", "native"]

_MAX_BROWSE_ENTRIES = 500


@dataclass(frozen=True)
class MountPoint:
    id: str
    path: Path
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        ok = False
        is_dir = False
        try:
            ok = self.path.exists()
            is_dir = self.path.is_dir() if ok else False
        except OSError:
            ok = False
            is_dir = False
        return {
            "id": self.id,
            "path": str(self.path),
            "label": self.label or self.id,
            "ok": bool(ok and is_dir),
            "exists": bool(ok),
            "is_dir": bool(is_dir),
        }


def detect_docker() -> bool:
    """Best-effort: /.dockerenv or cgroup mentions docker/containerd/podman."""
    try:
        if Path("/.dockerenv").exists():
            return True
    except OSError:
        pass
    try:
        cg = Path("/proc/1/cgroup")
        if cg.is_file():
            text = cg.read_text(encoding="utf-8", errors="ignore").lower()
            if any(tok in text for tok in ("docker", "containerd", "kubepods", "podman")):
                return True
    except OSError:
        pass
    # Windows containers / rare hosts
    if os.environ.get("DOTNET_RUNNING_IN_CONTAINER", "").strip() == "1":
        return True
    return False


def effective_runtime_mode() -> RuntimeMode:
    raw = (getattr(settings, "runtime_mode", None) or "auto").strip().lower()
    if raw in ("docker", "native"):
        return raw  # type: ignore[return-value]
    return "docker" if detect_docker() else "native"


def parse_mount_points(raw: str | list | tuple | None = None) -> list[MountPoint]:
    """Parse MOUNT_POINTS: id=/path,id2=/path2 or id=/path|Label.

    Separators between entries: comma or semicolon.
    Optional label after ``|``.
    """
    if raw is None:
        raw = getattr(settings, "mount_points", None) or []
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = [p.strip() for p in re.split(r"[;,]", str(raw or "")) if p.strip()]

    out: list[MountPoint] = []
    seen: set[str] = set()
    for item in items:
        label = ""
        body = item
        if "|" in item:
            body, label = item.split("|", 1)
            body, label = body.strip(), label.strip()
        if "=" not in body:
            log.warning("MOUNT_POINTS: skip entry without id=path: %r", item)
            continue
        mid, mpath = body.split("=", 1)
        mid = mid.strip()
        mpath = mpath.strip().strip('"').strip("'")
        if not mid or not mpath:
            log.warning("MOUNT_POINTS: skip empty id/path: %r", item)
            continue
        key = mid.casefold()
        if key in seen:
            log.warning("MOUNT_POINTS: duplicate id %r ignored", mid)
            continue
        seen.add(key)
        out.append(MountPoint(id=mid, path=Path(mpath), label=label or mid))
    return out


def list_mounts() -> list[dict[str, Any]]:
    return [m.as_dict() for m in parse_mount_points()]


def mount_roots() -> list[Path]:
    """Resolved roots that exist as directories (for allowlist)."""
    roots: list[Path] = []
    for m in parse_mount_points():
        try:
            if m.path.is_dir():
                roots.append(m.path.resolve())
        except OSError:
            continue
    return roots


def allowed_roots_for_import() -> tuple[list[Path], str]:
    """Roots that constrain import-path.

    Returns (roots, source) where source is 'mounts' | 'path_allowed_roots' | 'none'.
    In docker runtime with MOUNT_POINTS — only those mount dirs (empty existing → deny).
    Otherwise PATH_ALLOWED_ROOTS; else unrestricted.
    """
    if effective_runtime_mode() == "docker" and parse_mount_points():
        return mount_roots(), "mounts"
    roots = []
    for r in settings.path_allowed_roots or []:
        s = str(r).strip()
        if not s:
            continue
        try:
            roots.append(Path(s).resolve())
        except OSError:
            roots.append(Path(s))
    if roots:
        return roots, "path_allowed_roots"
    return [], "none"


def path_is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def find_mount_for_path(path: Path) -> MountPoint | None:
    best: MountPoint | None = None
    best_len = -1
    for m in parse_mount_points():
        try:
            if not m.path.is_dir():
                continue
            if path_is_inside(path, m.path):
                plen = len(str(m.path.resolve()))
                if plen > best_len:
                    best = m
                    best_len = plen
        except OSError:
            continue
    return best


def get_mount(mount_id: str) -> MountPoint | None:
    want = (mount_id or "").strip().casefold()
    if not want:
        return None
    for m in parse_mount_points():
        if m.id.casefold() == want:
            return m
    return None


def resolve_under_mount(mount_id: str, rel: str = "") -> Path:
    """Join mount root with relative path; reject .. and escape."""
    m = get_mount(mount_id)
    if m is None:
        raise ValueError(f"unknown mount id: {mount_id}")
    try:
        root = m.path.resolve()
    except OSError as exc:
        raise ValueError(f"mount path not accessible: {m.path}") from exc
    if not root.is_dir():
        raise ValueError(f"mount is not an available directory: {m.id} ({m.path})")

    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return root
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError("path must not contain '..'")
    target = root.joinpath(*parts)
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ValueError(f"path not accessible: {target}") from exc
    if not path_is_inside(resolved, root):
        raise ValueError("path escapes mount root")
    return resolved


def browse_mount(mount_id: str, rel: str = "") -> dict[str, Any]:
    """List directories and .txt files under a mount (for Docker UI picker)."""
    target = resolve_under_mount(mount_id, rel)
    if not target.is_dir():
        raise ValueError("browse path is not a directory")

    m = get_mount(mount_id)
    assert m is not None
    root = m.path.resolve()
    try:
        rel_out = str(target.relative_to(root)).replace("\\", "/")
        if rel_out == ".":
            rel_out = ""
    except ValueError:
        rel_out = ""

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
    except OSError as exc:
        raise ValueError(f"cannot list directory: {exc}") from exc

    for child in children:
        if len(entries) >= _MAX_BROWSE_ENTRIES:
            break
        name = child.name
        if name.startswith("."):
            continue
        try:
            if child.is_dir():
                kind = "dir"
                selectable = True
                hint = ""
                if (child / "Configuration.xml").is_file():
                    hint = "dump"
                entries.append(
                    {
                        "name": name,
                        "kind": kind,
                        "path": str(child.resolve()),
                        "rel": f"{rel_out}/{name}".strip("/") if rel_out else name,
                        "selectable": selectable,
                        "hint": hint,
                    }
                )
            elif child.is_file() and child.suffix.lower() == ".txt":
                entries.append(
                    {
                        "name": name,
                        "kind": "file",
                        "path": str(child.resolve()),
                        "rel": f"{rel_out}/{name}".strip("/") if rel_out else name,
                        "selectable": True,
                        "hint": "report",
                    }
                )
        except OSError:
            continue

    parent_rel = ""
    if rel_out:
        parent_rel = "/".join(rel_out.split("/")[:-1])

    return {
        "mount_id": m.id,
        "root": str(root),
        "path": str(target),
        "rel": rel_out,
        "parent_rel": parent_rel,
        "entries": entries,
        "truncated": len(entries) >= _MAX_BROWSE_ENTRIES,
    }


def suggest_paths_for_missing(source_path: str) -> list[dict[str, Any]]:
    """If old path is missing, suggest mount+same folder name under mounts."""
    raw = (source_path or "").strip()
    if not raw:
        return []
    src = Path(raw)
    try:
        if src.exists():
            return []
    except OSError:
        pass

    folder = src.name.strip() or ""
    if not folder or folder in (".", ".."):
        # try parent name for files
        folder = src.parent.name if src.parent else ""
    if not folder or folder in (".", ".."):
        return []

    suggestions: list[dict[str, Any]] = []
    folder_cf = folder.casefold()
    for m in parse_mount_points():
        try:
            if not m.path.is_dir():
                continue
            root = m.path.resolve()
        except OSError:
            continue
        # mount root itself named like the missing folder
        candidates: list[Path] = []
        if root.name.casefold() == folder_cf:
            candidates.append(root)
        candidates.append(root / folder)
        # also scan one level deep
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name.casefold() == folder_cf:
                    candidates.append(child)
                if child.is_dir():
                    nested = child / folder
                    if nested.is_dir() or nested.is_file():
                        candidates.append(nested)
        except OSError:
            pass

        seen: set[str] = set()
        for c in candidates:
            try:
                if not c.exists():
                    continue
                key = str(c.resolve())
                if key in seen:
                    continue
                seen.add(key)
                if not path_is_inside(c, root):
                    continue
                hint = ""
                if c.is_dir() and (c / "Configuration.xml").is_file():
                    hint = "dump"
                elif c.is_file() and c.suffix.lower() == ".txt":
                    hint = "report"
                elif c.is_dir():
                    hint = "dir"
                suggestions.append(
                    {
                        "mount_id": m.id,
                        "label": m.label or m.id,
                        "path": key,
                        "name": c.name,
                        "hint": hint,
                        "score": 2 if c.parent == root else 1,
                    }
                )
            except OSError:
                continue

    suggestions.sort(key=lambda x: (-int(x.get("score") or 0), str(x.get("path") or "")))
    # drop internal score
    for s in suggestions:
        s.pop("score", None)
    return suggestions[:12]


def runtime_snapshot() -> dict[str, Any]:
    mode = effective_runtime_mode()
    mounts = list_mounts()
    configured = bool(parse_mount_points())
    pick_native = mode == "native"
    return {
        "mode": mode,
        "mode_setting": (getattr(settings, "runtime_mode", None) or "auto").strip().lower() or "auto",
        "detected_docker": detect_docker(),
        "mounts_configured": configured,
        "mounts": mounts,
        "pick_path_available": pick_native,
        "path_delivery": "mounts" if (mode == "docker" and configured) else "native_path",
    }
