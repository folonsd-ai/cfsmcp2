"""Detect and validate 1C platform installations (Windows)."""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("cfsmcp2.onec_platform")

_VERSION_DIR_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)$")

_WIN_PLATFORM_ROOTS = (
    Path(r"C:\Program Files\1cv8"),
    Path(r"C:\Program Files (x86)\1cv8"),
)


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    path: Path
    version: str
    bitness: str  # "64" | "32" | "unknown"


def _parse_version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _exe_bitness(exe: Path) -> str:
    if sys.platform != "win32":
        return "unknown"
    try:
        import struct

        with exe.open("rb") as fh:
            fh.seek(0x3C)
            pe_offset = struct.unpack("<I", fh.read(4))[0]
            fh.seek(pe_offset + 4)
            machine = struct.unpack("<H", fh.read(2))[0]
        if machine == 0x8664:
            return "64"
        if machine in (0x014C, 0x01C0):
            return "32"
    except Exception:
        log.debug("bitness probe failed for %s", exe, exc_info=True)
    return "unknown"


def format_file_version_ms_ls(file_version_ms: int, file_version_ls: int) -> str:
    """Format VS_FIXEDFILEINFO dwFileVersionMS/LS as ``major.minor.build.revision``."""
    return (
        f"{(file_version_ms >> 16) & 0xFFFF}.{file_version_ms & 0xFFFF}."
        f"{(file_version_ls >> 16) & 0xFFFF}.{file_version_ls & 0xFFFF}"
    )


def _read_file_version(exe: Path) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
            ]

        ver_dll = ctypes.windll.version
        size = ver_dll.GetFileVersionInfoSizeW(str(exe), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver_dll.GetFileVersionInfoW(str(exe), 0, size, buf):
            return ""
        val = ctypes.c_void_p()
        val_len = wintypes.UINT()
        if not ver_dll.VerQueryValueW(buf, "\\", ctypes.byref(val), ctypes.byref(val_len)):
            return ""
        if val_len.value < ctypes.sizeof(VS_FIXEDFILEINFO):
            return ""
        info = ctypes.cast(val, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        if info.dwSignature != 0xFEEF04BD:
            return ""
        return format_file_version_ms_ls(info.dwFileVersionMS, info.dwFileVersionLS)
    except Exception:
        log.debug("file version read failed for %s", exe, exc_info=True)
        return ""


def _version_from_parent_dir(exe: Path) -> str:
    # .../8.3.24.1738/bin/1cv8.exe
    parent = exe.parent.parent.name if exe.parent.name.lower() == "bin" else exe.parent.name
    if _VERSION_DIR_RE.match(parent):
        return parent
    return ""


def validate_platform(path: str | Path) -> PlatformInfo | None:
    """Return platform info when *path* is an existing 1cv8.exe."""
    exe = Path(path)
    try:
        exe = exe.resolve()
    except OSError:
        exe = Path(path)
    if not exe.is_file():
        return None
    if exe.name.lower() != "1cv8.exe":
        return None
    file_ver = _read_file_version(exe)
    dir_ver = _version_from_parent_dir(exe)
    if dir_ver and (not file_ver or not _VERSION_DIR_RE.match(file_ver)):
        version = dir_ver
    else:
        version = file_ver or dir_ver
    if not version:
        version = "unknown"
    return PlatformInfo(path=exe, version=version, bitness=_exe_bitness(exe))


def detect_platforms() -> list[PlatformInfo]:
    """Scan standard install roots; newest version first."""
    found: dict[str, PlatformInfo] = {}
    if sys.platform != "win32":
        return []
    for root in _WIN_PLATFORM_ROOTS:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or not _VERSION_DIR_RE.match(child.name):
                continue
            exe = child / "bin" / "1cv8.exe"
            info = validate_platform(exe)
            if info is None:
                continue
            key = str(info.path).lower()
            found[key] = info
    return sorted(found.values(), key=lambda p: _parse_version_tuple(p.version), reverse=True)
