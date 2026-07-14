@echo off
setlocal

rem Always run from this script's own folder, regardless of where it was
rem launched from (double-click, shortcut, another directory, etc.).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe not found.
    echo Run "uv sync" in this folder first to create the virtual environment.
    pause
    exit /b 1
)

echo Starting GUV-CFD server...
echo Once it's running, open http://127.0.0.1:8050/ in your browser.
echo Press Ctrl+C to stop the server.
echo.

".venv\Scripts\python.exe" -m guvcfd.app

echo.
echo Server stopped.
pause
