@echo off
REM Launches the native GUV-CFD desktop app (PySide6/Qt), as opposed to
REM the web/Dash app (guvcfd/app.py). Double-click to run.
cd /d "%~dp0"
uv run python -m guvcfd.qtapp
if errorlevel 1 (
    echo.
    echo GUV-CFD PC app exited with an error - see above.
    pause
)
