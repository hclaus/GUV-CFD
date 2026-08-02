@echo off
REM Simple launcher for interactive tracer analysis - no command line needed!
cd /d "%~dp0"
python tracer_analyze.py
pause
