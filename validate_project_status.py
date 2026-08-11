#!/usr/bin/env python3
"""One-off, throwaway validation of the new project_status.py feature
against REAL project settings (4x5x31B1's own .guv/.guvcfd) - deliberately
targets a FRESH temp case directory, never touches the real
4x5x31B1/4x5x31B1ss/4x5x31B1ssv2 directories. Iteration counts reduced for
speed - this is validating that the STATUS FILE gets written correctly,
not producing physically meaningful results.

Usage: uv run python validate_project_status.py
"""
import json
from pathlib import Path

from guv_calcs import Project

from guvcfd.app_settings import load_advanced_settings, merge_project_openfoam_settings
from guvcfd.project_status import load_project_status
from guvcfd.scenario_runs import run_sweep
from guvcfd.visualization import center_frac_for_wall

GUV_PATH = r"C:\Users\hukcl\Documents\OpenFoam\4x5x3\4x5x31B1.guv"
SETTINGS_PATH = r"C:\Users\hukcl\Documents\OpenFoam\4x5x3\4x5x31B1SS.guvcfd"
PROJECT_DIR = r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\validate_project_status_tmp"

with open(SETTINGS_PATH) as f:
    settings = json.load(f)

# Fast-mode overrides - real geometry/inlet/outlet/ACH from the real
# project, but small enough to actually finish quickly for validation.
settings["case-dir"] = PROJECT_DIR
settings["phase1-iterations"] = 60
settings["phase2-iterations"] = 60
settings["mesh-cell-size"] = 0.15  # coarser mesh, faster mesh gen + convergence

project = Project.load(GUV_PATH)
room = next(iter(project.rooms.values()))
adv = merge_project_openfoam_settings(settings, load_advanced_settings())

print(f"=== Running a small validation sweep into {PROJECT_DIR} ===")
log_lines = []
run_sweep(
    GUV_PATH, SETTINGS_PATH, PROJECT_DIR, room, settings, adv,
    z_values=[settings["z-value"]], ach_values=[settings["ach"]],
    log_fn=lambda m: (print(m), log_lines.append(m)),
)

print("\n=== Resulting project_status.json ===")
status = load_project_status(PROJECT_DIR, Path(PROJECT_DIR).name)
print(json.dumps(status, indent=2))
