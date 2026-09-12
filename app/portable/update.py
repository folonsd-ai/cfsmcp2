"""Check GitHub Releases for a newer portable build."""

from __future__ import annotations

import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.version import APP_VERSION

GITHUB_REPO = "folonsd-ai/cfsmcp2"
GITHUB_HOME = f"https://github.com/{GITHUB_REPO}"
RELEASES_PAGE = f"{GITHUB_HOME}/releases/latest"
LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
PRESERVE_NAMES = frozenset({"data", "_updates", "cfsmcp2.ini"})


@dataclass
class UpdateInfo:
    current: str
    latest: str
    newer: bool
    release_url: str
    download_url: str | None
    download_name: str | None
    error: str | None = None


def _parse_version(text: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", text)]
    return tuple(nums or [0])


def _is_newer(current: str, latest: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _pick_portable_asset(assets: list[dict]) -> tuple[str | None, str | None]:
    patterns = (
        "cfsmcp2-win-portable",
        "win-portable",
        "portable-win",
        "windows-portable",
    )
    for asset in assets:
        name = str(asset.get("name") or "")
        low = name.casefold()
        if not low.endswith(".zip"):
            continue
        if any(p in low for p in patterns):
            return str(asset.get("browser_download_url") or ""), name
    for asset in assets:
        name = str(asset.get("name") or "")
        if name.casefold().endswith(".zip"):
            return str(asset.get("browser_download_url") or ""), name
    return None, None


def _no_update_info(current: str) -> UpdateInfo:
    return UpdateInfo(
        current=current,
        latest=current,
        newer=False,
        release_url=RELEASES_PAGE,
        download_url=None,
        download_name=None,
    )


def check_github_update(timeout: float = 20.0) -> UpdateInfo:
    current = APP_VERSION
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                LATEST_API,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "cfsmcp2-portable"},
            )
            if resp.status_code == 404:
                return _no_update_info(current)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _no_update_info(current)
        return UpdateInfo(
            current=current,
            latest=current,
            newer=False,
            release_url=RELEASES_PAGE,
            download_url=None,
            download_name=None,
            error=str(exc),
        )
    except Exception as exc:
        return UpdateInfo(
            current=current,
            latest=current,
            newer=False,
            release_url=RELEASES_PAGE,
            download_url=None,
            download_name=None,
            error=str(exc),
        )

    tag = str(data.get("tag_name") or data.get("name") or "").lstrip("v")
    latest = tag or current
    dl_url, dl_name = _pick_portable_asset(list(data.get("assets") or []))
    html_url = str(data.get("html_url") or RELEASES_PAGE)
    return UpdateInfo(
        current=current,
        latest=latest,
        newer=_is_newer(current, latest),
        release_url=html_url,
        download_url=dl_url,
        download_name=dl_name,
    )


def download_update_zip(url: str, dest: Path, timeout: float = 300.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    return dest


def find_portable_root(extracted: Path) -> Path:
    """Return directory that contains cfsmcp2.exe."""
    if (extracted / "cfsmcp2.exe").is_file():
        return extracted
    for child in sorted(extracted.iterdir()):
        if child.is_dir() and (child / "cfsmcp2.exe").is_file():
            return child
    raise ValueError("В архиве не найден каталог portable-сборки (cfsmcp2.exe).")


def extract_portable_zip(zip_path: Path, dest_dir: Path) -> Path:
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest_dir)
    return find_portable_root(dest_dir)


def apply_portable_update(source_root: Path, install_root: Path) -> None:
    """Copy portable build over install dir; keep data/, cfsmcp2.ini and _updates/."""
    if not (source_root / "cfsmcp2.exe").is_file():
        raise ValueError(f"Источник обновления не содержит cfsmcp2.exe: {source_root}")
    install_root.mkdir(parents=True, exist_ok=True)
    for item in source_root.iterdir():
        if item.name in PRESERVE_NAMES:
            continue
        dest = install_root / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)


def wait_for_process(pid: int, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.25)
    raise TimeoutError(f"Процесс {pid} не завершился за {int(timeout)} с")


def pending_update_dir(install_root: Path, version: str) -> Path:
    safe = re.sub(r"[^\w.\-]+", "_", version.strip()) or "pending"
    return install_root / "_updates" / "pending" / safe


def find_pending_staging(install_root: Path) -> Path | None:
    pending = install_root / "_updates" / "pending"
    if not pending.is_dir():
        return None
    found: list[tuple[float, Path]] = []
    for version_dir in pending.iterdir():
        if not version_dir.is_dir():
            continue
        try:
            root = find_portable_root(version_dir)
        except ValueError:
            continue
        found.append((version_dir.stat().st_mtime, root))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[-1][1]


def apply_pending_update_if_any(install_root: Path) -> bool:
    staging = find_pending_staging(install_root)
    if staging is None:
        return False
    apply_portable_update(staging, install_root)
    cleanup_pending_staging(staging, install_root)
    return True


def cleanup_pending_staging(source: Path, install_root: Path) -> None:
    pending = install_root / "_updates" / "pending"
    try:
        rel = source.relative_to(pending)
    except ValueError:
        return
    if not rel.parts:
        return
    version_dir = pending / rel.parts[0]
    if version_dir.exists():
        shutil.rmtree(version_dir, ignore_errors=True)
