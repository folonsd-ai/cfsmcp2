@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-portable.ps1" -Target "%~dp0"
if errorlevel 1 (
    echo.
    echo Update failed. See _updates\apply-update.log
    pause
    exit /b 1
)
