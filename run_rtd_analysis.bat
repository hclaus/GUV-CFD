@echo off
REM RTD analysis launcher - extracts residence time distribution from age field
cd /d "%~dp0"
python tracer_analyze_simple.py
pause
