#!/usr/bin/env python3
"""Launch the Sandberg replicate project's ACH=1/2/4 decay sweep, at the
COARSE (0.1m, ~40,824-cell) mesh resolution - replaces the original
sandberg_test_room.guvcfd (0.05m, 326,592 cells) attempt, which repeatedly
hit multi-hour convergence times and WSL filesystem-consistency issues
under sustained load. The original project/case directory is left
untouched on disk in case it's revisited later.

Mirrors run_sandberg_sweep.py exactly, except: (a) points at
sandberg_test_room_coarse.guvcfd, and (b) merges the per-project
mesh-cell-size (0.1m, explicitly pinned in that file) over whatever the
global advanced settings currently default to - see
app_settings.merge_project_openfoam_settings, built specifically so a
project's own settings can never again silently drift with the global
default (which is exactly what caused the original 0.05m mesh in the
first place - see project_per_project_openfoam_settings memory).
"""
import json
import sys

from guv_calcs import Project

from guvcfd import scenario_runs
from guvcfd.app_settings import load_advanced_settings, merge_project_openfoam_settings

SETTINGS_PATH = r"C:\Users\hukcl\Documents\OpenFoam\Sandberg_simulations\sandberg_test_room_coarse.guvcfd"

with open(SETTINGS_PATH) as f:
    settings = json.load(f)

guv_path = settings["guv_path"]
project = Project.load(guv_path)
room = next(iter(project.rooms.values()))
adv = merge_project_openfoam_settings(settings, load_advanced_settings())
print(f"mesh-cell-size (per-project, pinned): {adv['mesh-cell-size']}m")

z_values = [float(v) for v in settings["scenario-z-values"].split(",")]
ach_values = [float(v) for v in settings["scenario-ach-values"].split(",")]
combos = scenario_runs.sweep_combinations(z_values, ach_values)
print(f"Combos: {combos}")
print(f"Case dir: {settings['case-dir']}")
print(f"pimple-end-time: {settings['pimple-end-time']}s, write-interval: {settings['pimple-write-interval']}s")
sys.stdout.flush()

results = {}


def on_combo_done(z, ach, status, detail):
    results[(z, ach)] = {"status": status, "detail": detail}
    print(f"COMBO DONE: Z={z} ACH={ach} status={status}")
    sys.stdout.flush()


def log_fn(line):
    print(line)
    sys.stdout.flush()


def solver_log_fn(line):
    if line.strip().startswith("["):
        print(line)
        sys.stdout.flush()


try:
    scenario_runs.run_decay_sweep(
        guv_path, SETTINGS_PATH, settings["case-dir"], room, settings, adv,
        z_values, ach_values, log_fn=log_fn, on_combo_done=on_combo_done,
        solver_log_fn=solver_log_fn,
    )
    print("SWEEP FINISHED")
except Exception as e:
    print(f"SWEEP ERROR: {e}")
    raise
finally:
    print("=== SUMMARY ===")
    for (z, ach), r in sorted(results.items()):
        print(f"Z={z} ACH={ach}: {r['status']}")
