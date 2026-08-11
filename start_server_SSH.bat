@echo off
setlocal

rem Same as start_server.bat, but tells the app to talk to WSL over SSH
rem instead of the normal method - an experimental option, see
rem "Linux installation.md" for what this means and how to set it up.
rem Use the plain start_server.bat instead if you don't want this.

rem Always run from this script's own folder, regardless of where it was
rem launched from (double-click, shortcut, another directory, etc.).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe not found.
    echo Run "uv sync" in this folder first to create the virtual environment.
    pause
    exit /b 1
)

echo Starting GUV-CFD server with the experimental SSH connection to WSL...
echo (GUVCFD_WSL_TRANSPORT=ssh - see "Linux installation.md")
echo Once it's running, open http://127.0.0.1:8050/ in your browser.
echo Press Ctrl+C to stop the server.
echo.

set GUVCFD_WSL_TRANSPORT=ssh
".venv\Scripts\python.exe" -m guvcfd.app

echo.
echo Server stopped.
pause
