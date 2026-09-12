"""Check GitHub Releases for a newer portable build."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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
TAGS_API = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "cfsmcp2-portable"}
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


def _no_update_info(current: str, latest: str | None = None) -> UpdateInfo:
    shown = latest or current
    return UpdateInfo(
        current=current,
        latest=shown,
        newer=False,
        release_url=RELEASES_PAGE,
        download_url=None,
        download_name=None,
    )


def _version_from_tag(name: str) -> str:
    return str(name or "").strip().lstrip("vV")


def _best_tag_version(tags: list[dict]) -> str | None:
    best: str | None = None
    best_tuple = (0,)
    for item in tags:
        ver = _version_from_tag(str(item.get("name") or ""))
        if not ver:
            continue
        parsed = _parse_version(ver)
        if parsed > best_tuple:
            best_tuple = parsed
            best = ver
    return best


def _version_from_remote_repo(client: httpx.Client) -> str | None:
    for branch in ("master", "main"):
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/app/core/version.py"
        try:
            resp = client.get(url, headers={"User-Agent": "cfsmcp2-portable"})
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)', resp.text)
        if match:
            return match.group(1).strip()
    return None


def _release_url_for_version(latest: str, has_release: bool) -> str:
    if has_release:
        return RELEASES_PAGE
    tag_url = f"{GITHUB_HOME}/releases/tag/v{latest}"
    return tag_url


def _fetch_latest_release(client: httpx.Client) -> dict | None:
    resp = client.get(LATEST_API, headers=GITHUB_HEADERS)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _resolve_latest_version_without_release(client: httpx.Client) -> str | None:
    resp = client.get(TAGS_API, params={"per_page": 100}, headers=GITHUB_HEADERS)
    resp.raise_for_status()
    tag_ver = _best_tag_version(list(resp.json()))
    if tag_ver:
        return tag_ver
    return _version_from_remote_repo(client)


def check_github_update(timeout: float = 20.0) -> UpdateInfo:
    current = APP_VERSION
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            release = _fetch_latest_release(client)
            if release is not None:
                latest = _version_from_tag(str(release.get("tag_name") or release.get("name") or "")) or current
                dl_url, dl_name = _pick_portable_asset(list(release.get("assets") or []))
                html_url = str(release.get("html_url") or RELEASES_PAGE)
                return UpdateInfo(
                    current=current,
                    latest=latest,
                    newer=_is_newer(current, latest),
                    release_url=html_url,
                    download_url=dl_url,
                    download_name=dl_name,
                )

            latest = _resolve_latest_version_without_release(client)
            if latest is None or not _is_newer(current, latest):
                return _no_update_info(current, latest)

            return UpdateInfo(
                current=current,
                latest=latest,
                newer=True,
                release_url=_release_url_for_version(latest, False),
                download_url=None,
                download_name=None,
            )
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


def _staging_version_dir(source: Path, install_root: Path) -> Path | None:
    pending = install_root / "_updates" / "pending"
    try:
        rel = source.relative_to(pending)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return pending / rel.parts[0]


def _asset_template(name: str) -> Path:
    here = Path(__file__).resolve().parent
    bundled = here / "assets" / name
    if bundled.is_file():
        return bundled
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen = Path(meipass) / "app" / "portable" / "assets" / name
        if frozen.is_file():
            return frozen
    raise FileNotFoundError(f"{name} not found in portable assets")


def _windows_update_script_template() -> Path:
    return _asset_template("apply-update.ps1")


def _ensure_windows_update_script(install_root: Path) -> Path:
    updates_dir = install_root / "_updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    dest = updates_dir / "apply-update.ps1"
    template = _windows_update_script_template()
    content = template.read_text(encoding="utf-8")
    if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
        dest.write_text(content, encoding="utf-8")
    return dest


def prepare_update_helper(install_root: Path, staging: Path | None = None) -> None:
    """Copy update scripts to install dir (safe while launcher is running)."""
    if os.name != "nt":
        return
    _ensure_windows_update_script(install_root)
    updates_dir = install_root / "_updates"
    for name in ("update-portable.ps1", "update-portable.cmd", "apply-update.ps1"):
        try:
            template = _asset_template(name)
        except FileNotFoundError:
            continue
        content = template.read_text(encoding="utf-8")
        for dest in (install_root / name, updates_dir / name):
            if dest.parent != install_root and name == "apply-update.ps1":
                continue
            if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
                dest.write_text(content, encoding="utf-8")
    if staging is not None:
        staging_script = staging / "apply-update.ps1"
        if staging_script.is_file():
            helper = install_root / "apply-update.ps1"
            content = staging_script.read_text(encoding="utf-8")
            if not helper.is_file() or helper.read_text(encoding="utf-8") != content:
                helper.write_text(content, encoding="utf-8")


def find_staging_updater_exe(source: Path, install_exe: Path) -> Path | None:
    staging_exe = source / "cfsmcp2.exe"
    if not staging_exe.is_file():
        return None
    try:
        if staging_exe.resolve() == install_exe.resolve():
            return None
    except OSError:
        return staging_exe
    return staging_exe


def spawn_staging_apply_update(source: Path, target: Path, wait_pid: int) -> None:
    """Run --apply-update from staged build (works when install exe is older)."""
    staging_exe = find_staging_updater_exe(source, Path(sys.executable))
    if staging_exe is None:
        raise FileNotFoundError("staged cfsmcp2.exe not found for bootstrap update")
    prepare_update_helper(target, source)
    flags = 0x08000000 if os.name == "nt" else 0
    cmd = [
        str(staging_exe),
        "--apply-update",
        f"--source={source}",
        f"--target={target}",
        f"--wait-pid={wait_pid}",
    ]
    subprocess.Popen(cmd, cwd=str(target), creationflags=flags)


def spawn_windows_update_apply(source: Path, target: Path, wait_pid: int) -> None:
    """Apply update via PowerShell helper (exe must not run from target during copy)."""
    if not (source / "cfsmcp2.exe").is_file():
        raise ValueError(f"Источник обновления не содержит cfsmcp2.exe: {source}")
    script = _ensure_windows_update_script(target)
    log_path = target / "_updates" / "apply-update.log"
    version_dir = _staging_version_dir(source, target)
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script),
        "-Source",
        str(source),
        "-Target",
        str(target),
        "-WaitPid",
        str(wait_pid),
        "-LogFile",
        str(log_path),
    ]
    if version_dir is not None:
        cmd.extend(["-StagingVersionDir", str(version_dir)])
    flags = 0x08000000 if os.name == "nt" else 0
    subprocess.Popen(cmd, cwd=str(target), creationflags=flags)


def apply_portable_update(source_root: Path, install_root: Path) -> None:
    """Copy portable build over install dir; keep data/, cfsmcp2.ini and _updates/."""
    if os.name == "nt":
        raise RuntimeError("apply_portable_update in-process is unsafe on Windows; use spawn_windows_update_apply")
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


def apply_pending_update_if_any(install_root: Path, wait_pid: int = 0) -> bool:
    staging = find_pending_staging(install_root)
    if staging is None:
        return False
    if os.name == "nt":
        spawn_windows_update_apply(staging, install_root, wait_pid)
        return True
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
