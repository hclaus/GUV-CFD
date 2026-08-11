#!/usr/bin/env python3
"""Launch the Sandberg replicate project's ACH=1/2/4 decay sweep. Mirrors
app.py's own _scenario_sweep_thread call pattern exactly (log_fn=print for
narration, solver_log_fn filtered to only run_wsl_streaming's own bracketed
stall/retry diagnostics - the same split that already keeps the app's own
scenario log from flooding with raw per-iteration solver stdout).
"""
import json
import sys

from guv_calcs import Project

from guvcfd import scenario_runs
from guvcfd.app_settings import load_advanced_settings

SETTINGS_PATH = r"C:\Users\hukcl\Documents\OpenFoam\Sandberg_simulations\sandberg_test_room.guvcfd"

with open(SETTINGS_PATH) as f:
    settings = json.load(f)

guv_path = settings["guv_path"]
project = Project.load(guv_path)
room = next(iter(project.rooms.values()))
adv = load_advanced_settings()

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
