@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
if not exist "%ROOT%cfsmcp2.exe" (
    if exist "%ROOT%..\cfsmcp2.exe" (
        cd /d "%ROOT%.."
        set "ROOT=%CD%\"
    )
)
if not exist "%ROOT%cfsmcp2.exe" (
    echo Install root not found. Run from cfsmcp2 folder or _updates subfolder.
    pause
    exit /b 1
)
if not exist "%ROOT%update-portable.ps1" (
    echo update-portable.ps1 not found in %ROOT%
    pause
    exit /b 1
)
cd /d "%ROOT%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\update-portable.ps1" -Target "%CD%"
if errorlevel 1 (
    echo.
    echo Update failed. See %CD%\_updates\apply-update.log
    pause
    exit /b 1
)
