@echo off
setlocal

rem Always run from this script's own folder, regardless of where it was
rem launched from (double-click, shortcut, another directory, etc.).
cd /d "%~dp0"

echo Stopping any running GUV-CFD server instances...
powershell -NoProfile -Command ^
    "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'guvcfd\.app' -or $_.CommandLine -match 'guvcfd\\app\.py' } | ForEach-Object { Write-Host ('  killing PID ' + $_.ProcessId + ': ' + $_.CommandLine); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

rem Give Windows a moment to release the port before rebinding it.
timeout /t 2 /nobreak >nul

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe not found.
    echo Run "uv sync" in this folder first to create the virtual environment.
    pause
    exit /b 1
)

echo.
echo Starting GUV-CFD server...
echo Once it's running, open http://127.0.0.1:8050/ in your browser.
echo Press Ctrl+C to stop the server.
echo.

".venv\Scripts\python.exe" -m guvcfd.app

echo.
echo Server stopped.
pause
