#!/usr/bin/env python3
"""Recover the 3 Sandberg _base_ACH{1,2,4} flow-convergence bases after the
2026-08-03 crash (read_vol_average_dat hit the WSL cross-boundary read gap
after ~6 hours of genuine progress - now fixed in decay_analysis.py, but
this run's own flow_convergence_history.json was silently never written at
all, due to the separate _save_history bug, also now fixed).

0/U on all 3 was confirmed to hold real, nonuniform (already well-developed)
converged field data, not a reset/uniform guess - so this resumes flow
convergence from that existing field via run_pipeline.resume_case_setup's
decision="continue" path (reuses mesh/fields/fvOptions on disk untouched,
does NOT redo mesh generation or potentialFoam), rather than re-running
setup_case() from scratch, which would discard the ~6 hours already spent.

Once each ACH's resume_case_setup() call succeeds, base_dir/0/fluenceRate
exists, and re-running run_sandberg_sweep.py's own normal path will detect
that (_build_flow_base's own reuse check) and skip straight to per-Z combo
processing - no further manual steps needed after this script.
"""
import json
import sys

from guv_calcs import Project

from guvcfd import scenario_runs
from guvcfd.app_settings import load_advanced_settings, merge_project_openfoam_settings
from guvcfd.run_pipeline import resume_case_setup, FlowConvergenceUndecided

SETTINGS_PATH = r"C:\Users\hukcl\Documents\OpenFoam\Sandberg_simulations\sandberg_test_room.guvcfd"

with open(SETTINGS_PATH) as f:
    settings = json.load(f)

guv_path = settings["guv_path"]
project = Project.load(guv_path)
room = next(iter(project.rooms.values()))
adv = merge_project_openfoam_settings(settings, load_advanced_settings())

ach_values = [float(v) for v in settings["scenario-ach-values"].split(",")]
project_dir = settings["case-dir"]

ADDITIONAL_ITERATIONS = 6000


def log_fn(line):
    print(line, flush=True)


def solver_log_fn(line):
    if line.strip().startswith("["):
        print(line, flush=True)


for ach in ach_values:
    base_dir = f"{project_dir}/_base_ACH{scenario_runs._ach_label(ach)}"
    print(f"\n=== ACH={ach}: resuming flow convergence in {base_dir} ===", flush=True)
    try:
        summary = resume_case_setup(
            base_dir, guv_path, decision="continue", ach=ach, Z=settings["z-value"],
            nbins=adv["uv-zone-bins"],
            inlet_wall=settings["inlet-wall"],
            inlet_center=scenario_runs._opening_center_frac(settings, "inlet", room),
            inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
            inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
            cell_size=adv["mesh-cell-size"],
            additional_iterations=ADDITIONAL_ITERATIONS,
            flow_rel_tol=adv["flow-rel-tol"] / 100.0,
            oscillation_window=adv["oscillation-window"],
            oscillation_growth_tol=adv["oscillation-growth-tol"],
            ach_delivery_tol=adv["ach-delivery-tol"] / 100.0,
            max_co=adv["max-co"],
            log_fn=log_fn, solver_log_fn=solver_log_fn,
        )
        print(f"ACH={ach}: RESOLVED - {summary.get('flow_converged')=} "
              f"{summary.get('ach_delivery')=}", flush=True)
    except FlowConvergenceUndecided as e:
        print(f"ACH={ach}: STILL UNDECIDED after {ADDITIONAL_ITERATIONS} more iterations - "
              f"{e.total_iterations} total. {e}", flush=True)
    except Exception as e:
        print(f"ACH={ach}: ERROR - {e}", flush=True)
        raise

print("\n=== DONE ===", flush=True)
