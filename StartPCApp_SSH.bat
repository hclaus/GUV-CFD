@echo off
REM Same as StartPCApp.bat, but tells the app to talk to WSL over SSH
REM instead of the normal method - an experimental option, see
REM "Linux installation.md" for what this means and how to set it up.
REM Use the plain StartPCApp.bat instead if you don't want this.
cd /d "%~dp0"
echo Starting with the experimental SSH connection to WSL (GUVCFD_WSL_TRANSPORT=ssh)...
set GUVCFD_WSL_TRANSPORT=ssh
uv run python -m guvcfd.qtapp
if errorlevel 1 (
    echo.
    echo GUV-CFD PC app exited with an error - see above.
    pause
)
