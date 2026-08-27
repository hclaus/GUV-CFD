@echo off
REM One-time (or repeat-safe) setup step: compiles the TClampDecay OpenFOAM
REM function object inside WSL - see guvcfd/tclamp_decay.py. Not required
REM before every run - the app compiles this itself automatically the
REM first time "Use T divergence clamp" is enabled in Settings. Run this
REM manually on a new install/machine to verify the WSL/OpenFOAM/compiler
REM toolchain up front, with clear pass/fail output.
cd /d "%~dp0"
uv run python -m guvcfd.build_tclamp_decay
if errorlevel 1 (
    echo.
    echo TClampDecay setup failed - see above.
    pause
) else (
    echo.
    pause
)
