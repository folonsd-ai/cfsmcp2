# PyInstaller spec: Windows portable onedir (single exe, --server subprocess).
# Run from repo root: pyinstaller packaging/cfsmcp2.spec

import os

from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules, copy_metadata

spec_dir = os.path.dirname(os.path.abspath(SPEC))
repo_root = os.path.abspath(os.path.join(spec_dir, ".."))
icon_path = os.path.join(spec_dir, "assets", "cfs-mark.ico")
rthook = os.path.join(spec_dir, "pyi_rth_portable.py")

hiddenimports = [
    "app.main",
    "app.portable.ini",
    "app.portable.update",
    "app.core.paths",
    "app.core.version",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.importer",
    "click",
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "httptools",
    "websockets",
    "watchfiles",
    "multipart",
    "sqlite3",
    "pystray",
    "PIL",
    "PIL._imaging",
    "pydantic_core",
    "pydantic_core._pydantic_core",
    "tkinter",
    "_tkinter",
]
hiddenimports += collect_submodules("fastmcp", filter=lambda name: ".cli" not in name)
hiddenimports += collect_submodules("mcp", filter=lambda name: ".cli" not in name)
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_settings")
hiddenimports += collect_submodules("email_validator")

_pydantic_core_datas, _pydantic_core_binaries, _pydantic_core_hidden = collect_all("pydantic_core")
hiddenimports += _pydantic_core_hidden

binaries = collect_dynamic_libs("zvec") + collect_dynamic_libs("pydantic_core") + _pydantic_core_binaries

datas = [(os.path.join(repo_root, "app", "static"), "app/static")] + _pydantic_core_datas
for _pkg in (
    "fastmcp",
    "fastmcp-slim",
    "fastmcp_slim",
    "mcp",
    "mcp-types",
    "mcp_types",
    "pydantic",
    "pydantic_core",
    "pydantic-settings",
    "pydantic_settings",
    "starlette",
    "uvicorn",
    "zvec",
):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass
if os.path.isfile(icon_path):
    datas.append((icon_path, "."))

a = Analysis(
    [os.path.join(repo_root, "app", "portable", "launcher.py")],
    pathex=[repo_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[rthook],
    excludes=["PIL._avif"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="cfsmcp2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=icon_path if os.path.isfile(icon_path) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="cfsmcp2-win-portable",
)
