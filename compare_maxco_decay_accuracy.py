#!/usr/bin/env python3
"""Quick test: does raising pimpleFoam's maxCo (the adaptive-timestep
Courant cap) during the ventilation-only DECAY run change the fitted
decay rate (-> measured ACH / eACH) it produces, or only its wall-clock
speed?

Motivated by 2026-08-06's live production speedup on the running
4x5x31B1 sweep (maxCo bumped 5 -> 7 -> 10 on the control + Z*_ACH* runs
while they were in flight). That experiment only confirmed numerical
STABILITY (flat continuity error, no divergence) - it says nothing about
whether the fitted decay curve itself is still ACCURATE at higher maxCo.
Web research (CFDpilot, cross-checked against the OpenFOAM PIMPLE guide)
places maxCo=10 at the very edge of the "still accurate" range for
nOuterCorrectors=3 (this template's setting - see controlDict's own
comment) - this script measures the actual effect directly instead of
trusting that guidance blindly.

Design: build ONE flow-converged base case - mirrors the real 4x5x31B1
project's own room/inlet/outlet/ACH=3 settings (see
Z6_ACH3/run_settings.json) so this is testing OUR actual geometry, not a
generic one - then clone it into one independent ventilation-only control
run per maxCo value (ventilation_control.prepare_ventilation_only_control,
the same "control" machinery the real mechanical-ACH-only pipeline uses -
see project_mechanical_ach_only_feature_2026-08-05 memory). Flow
convergence is paid for ONCE and shared, so every comparison isolates
JUST the decay-phase maxCo's effect on the fitted curve, not
flow-convergence noise.

Unlike compare_convergence_methods.py (which deliberately runs its
combinations ONE AT A TIME because it's comparing wall-clock speed
between methods, and concurrent CPU contention would contaminate that),
this script is comparing ACCURACY, not speed - so the 3 maxCo legs are
independent of each other once the shared base is built, and running
them concurrently on separate cores is both safe and strictly faster.
Each leg is a separate process invocation (--only-maxco N), so they can
genuinely run in parallel rather than one Python process looping serially.

Fully independent of the live production project - a throwaway case
directory under TEST_CASE_ROOT, never touches 4x5x31B1's own directories.

Usage:
  uv run python compare_maxco_decay_accuracy.py --build-base
      Builds the shared flow-converged base case only (do this once).
  uv run python compare_maxco_decay_accuracy.py --only-maxco 5
      Runs just the maxCo=5 leg against the already-built base - launch
      one of these per maxCo value, in parallel, as separate processes.
  uv run python compare_maxco_decay_accuracy.py --assemble
      Once every --only-maxco leg has finished, reads their individual
      result files and writes the final comparison summary.
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from guv_calcs import Project

from guvcfd.run_pipeline import setup_case
from guvcfd.ventilation_control import prepare_ventilation_only_control, finish_ventilation_only_control
from guvcfd.decay_analysis import read_vol_average_dat, fit_effective_decay_rate
from guvcfd.scenario_runs import _decay_run_durations
from guvcfd.visualization import center_frac_for_wall
from guvcfd.wsl_utils import wsl_path, run_wsl_streaming

# Mirrors C:/Users/hukcl/Documents/OpenFoam/4x5x3/4x5x31B1.guv +
# Z6_ACH3/run_settings.json exactly (room/inlet/outlet/ACH/mesh) - the
# actual project this whole investigation is about.
GUV_PATH = r"C:\Users\hukcl\Documents\OpenFoam\4x5x3\4x5x31B1.guv"
TEST_CASE_ROOT = r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\maxco_decay_compare"
TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "guvcfd" / "templates" / "case_template")

ACH = 3.0
CELL_SIZE = 0.1
INLET_WALL, INLET_Y, INLET_Z, INLET_W, INLET_H = "xMin", 2.5, 0.4, 0.2, 0.2
OUTLET_WALL, OUTLET_Y, OUTLET_Z, OUTLET_W, OUTLET_H = "xMax", 2.5, 2.7, 0.2, 0.2
MOMENTUM_RELAXATION, SCALAR_RELAXATION = 0.7, 0.7
SCALAR_TRANSPORT_NCORR, SCALAR_TRANSPORT_TOLERANCE = 3, 1e-4
PIMPLE_DELTA_T = 0.5

# 5 = original conservative default, 10 = today's live production setting,
# 15 = deliberately past the "still accurate" line found in research, as a
# calibration point for how big the effect gets once clearly too aggressive.
MAXCO_VALUES = [5, 10, 15]
ADV = {"decay-ach-min-fraction": 90.0, "decay-each-max-fraction": 99.9, "decay-each-min-fraction": 90.0}

BASE_DIR = f"{TEST_CASE_ROOT}/base"
BASE_SUMMARY_CACHE = Path(__file__).resolve().parent / "maxco_compare_base_summary.json"
NOTES_PATH = Path(__file__).resolve().parent / "maxco_decay_accuracy_NOTES.md"
RESULTS_PATH = Path(__file__).resolve().parent / "compare_maxco_decay_accuracy_results.json"


def _leg_result_path(max_co):
    return Path(__file__).resolve().parent / f"maxco_compare_leg_{max_co}.json"


def build_base():
    """Build the ONE shared flow-converged base case and cache its summary
    (inlet_velocity etc.) to disk so separate --only-maxco processes can
    read it back without repeating flow convergence themselves."""
    project = Project.load(GUV_PATH)
    room = next(iter(project.rooms.values()))
    inlet_center = center_frac_for_wall(INLET_WALL, INLET_Y, INLET_Z, room)
    outlet_center = center_frac_for_wall(OUTLET_WALL, OUTLET_Y, OUTLET_Z, room)
    combined_end_time, control_end_time = _decay_run_durations(ACH, 0.0, ADV)

    setup_note = f"""# maxCo effect on decay-mode accuracy

Started: {datetime.now().isoformat(timespec='seconds')}

**Purpose**: isolate the effect of pimpleFoam's `maxCo` (adaptive-timestep
Courant cap) on the FITTED ventilation decay rate (-> measured ACH), not
just on numerical stability (already confirmed separately on the live
production sweep). Uses the real 4x5x3 project's own room/inlet/outlet/
mesh/ACH=3 settings, mechanical-ACH-only (no UV), so this speaks directly
to that project's own results.

**Fixed across every maxCo value** (only maxCo itself varies):
- Room: {room.x}x{room.y}x{room.z} {room.units}, mesh cell size {CELL_SIZE}m
- Inlet: {INLET_WALL} wall, ({INLET_Y}, {INLET_Z})m, {INLET_W}x{INLET_H}m, ceiling diffuser
- Outlet: {OUTLET_WALL} wall, ({OUTLET_Y}, {OUTLET_Z})m, {OUTLET_W}x{OUTLET_H}m
- Nominal ACH: {ACH} /hr
- Decay run duration: {control_end_time:.1f}s (90% decay target, same rule
  production uses - see scenario_runs._decay_run_durations)
- pimple_delta_t (initial): {PIMPLE_DELTA_T}s, nOuterCorrectors=3 (template default, unchanged)
- momentum/scalar relaxation: {MOMENTUM_RELAXATION}/{SCALAR_RELAXATION}, scalarTransport nCorr={SCALAR_TRANSPORT_NCORR} tol={SCALAR_TRANSPORT_TOLERANCE}

**maxCo values tested**: {MAXCO_VALUES} - run CONCURRENTLY (separate
processes/cores) once this base is built, since this test compares
accuracy, not speed, so cross-run CPU contention doesn't invalidate it.

**Method**: ONE flow-converged base case is built once (mechanical-ACH-only,
so no UV/fluence pipeline involved), then cloned into one independent
ventilation-only control run per maxCo value
(ventilation_control.prepare_ventilation_only_control) - flow-convergence
cost is paid once and shared, so differences between runs below are due
to maxCo alone, not flow-convergence noise.

**Case directories**: `{TEST_CASE_ROOT}/base` (shared flow field),
`{TEST_CASE_ROOT}/control_maxco_<N>` (one per maxCo value).

Results (measured ACH from the fitted decay curve, fit uncertainty, and
wall-clock time) are appended below once every leg finishes.

---

## Results
"""
    NOTES_PATH.write_text(setup_note)
    print(f"Wrote setup notes to {NOTES_PATH}")
    print(f"Room: {room.x}x{room.y}x{room.z} {room.units}, cell size={CELL_SIZE}m, "
          f"ACH={ACH}, decay duration={control_end_time:.1f}s, maxCo values={MAXCO_VALUES}")

    print(f"\n=== Building shared flow-converged base case ({BASE_DIR}) ===")
    t0 = time.time()
    base_summary = setup_case(
        GUV_PATH, BASE_DIR, template_case_dir=TEMPLATE_CASE_DIR,
        Z=0.0, ach=ACH, nbins=1,
        inlet_wall=INLET_WALL, inlet_center=inlet_center, inlet_size=(INLET_W, INLET_H),
        inlet_diffuser_type="ceiling",
        outlet_wall=OUTLET_WALL, outlet_center=outlet_center, outlet_size=(OUTLET_W, OUTLET_H),
        cell_size=CELL_SIZE, mechanical_ach_only=True,
        momentum_relaxation=MOMENTUM_RELAXATION, scalar_relaxation=SCALAR_RELAXATION,
        scalar_transport_ncorr=SCALAR_TRANSPORT_NCORR, scalar_transport_tolerance=SCALAR_TRANSPORT_TOLERANCE,
        pimple_delta_t=PIMPLE_DELTA_T,
    )
    base_build_s = time.time() - t0
    print(f"--- base case ready in {base_build_s:.1f}s "
          f"(inlet_velocity={base_summary.get('inlet_velocity')}) ---")

    BASE_SUMMARY_CACHE.write_text(json.dumps({
        "inlet_velocity": base_summary.get("inlet_velocity"),
        "inlet2_velocity": base_summary.get("inlet2_velocity"),
        "base_build_s": base_build_s,
        "control_end_time": control_end_time,
    }, indent=2))
    print(f"Cached base summary to {BASE_SUMMARY_CACHE}")


def run_leg(max_co):
    """Run just ONE maxCo leg against the already-built base case - safe
    to run concurrently with other legs (separate control_dir each, no
    shared mutable state except the read-only base case)."""
    cached = json.loads(BASE_SUMMARY_CACHE.read_text())
    control_end_time = cached["control_end_time"]

    name = f"control_maxco_{max_co}"
    control_dir = f"{TEST_CASE_ROOT}/{name}"
    control_dir_wsl = wsl_path(control_dir)
    print(f"=== {name} (maxCo={max_co}) ===")
    t0 = time.time()

    prepare_ventilation_only_control(
        BASE_DIR, control_dir, cached["inlet_velocity"],
        control_end_time, pimple_write_interval=max(1, int(control_end_time // 20)),
        pimple_delta_t=PIMPLE_DELTA_T, max_co=max_co,
        inlet2_velocity=cached.get("inlet2_velocity"),
        has_outlet2=False,
    )

    print(f"[{name}] Running pimpleFoam (maxCo={max_co}, target {control_end_time:.1f}s)...")
    r = run_wsl_streaming(
        "pimpleFoam 2>&1 | tee log.pimpleFoam", control_dir_wsl,
        on_line=lambda line: print(f"[{name}] {line}") if "Time =" in line else None,
    )
    result = {"name": name, "max_co": max_co}
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        print(f"[{name}] *** FAILED (exit {r.returncode}) ***\n{tail}")
        result.update({"outcome": "FAILED", "detail": tail})
    else:
        control_results = finish_ventilation_only_control(control_dir, ACH, log_fn=lambda m: print(f"[{name}] {m}"))
        elapsed = time.time() - t0
        t, T = read_vol_average_dat(f"{control_dir}/postProcessing/volAverage1/0/volFieldValue.dat")
        fit = fit_effective_decay_rate(t, T)
        result.update({
            "outcome": "ok", "wall_clock_s": elapsed,
            "measured_ach": control_results["total_ach_effective"],
            "measured_ach_ci95": control_results.get("total_ach_effective_ci95"),
            "lambda_per_s": fit["lambda_per_s"], "se_per_s": fit["se_per_s"], "n_points": fit["n"],
        })
        print(f"[{name}] measured ACH={control_results['total_ach_effective']:.4g}/hr "
              f"(nominal {ACH}/hr), wall-clock={elapsed:.1f}s")

    _leg_result_path(max_co).write_text(json.dumps(result, indent=2))
    print(f"[{name}] wrote {_leg_result_path(max_co)}")


def assemble():
    cached = json.loads(BASE_SUMMARY_CACHE.read_text())
    results = []
    for max_co in MAXCO_VALUES:
        p = _leg_result_path(max_co)
        if not p.exists():
            print(f"WARNING: {p} missing - leg maxCo={max_co} hasn't finished yet, skipping it")
            continue
        results.append(json.loads(p.read_text()))

    baseline = next((r for r in results if r["max_co"] == MAXCO_VALUES[0] and r["outcome"] == "ok"), None)

    print("=== SUMMARY ===")
    summary_lines = [f"Base case build: {cached['base_build_s']:.1f}s"]
    print(summary_lines[0])
    for r in results:
        if r["outcome"] != "ok":
            line = f"  maxCo={r['max_co']:<4} FAILED - {r['detail'][:200]}"
        else:
            pct = (f"{(r['measured_ach'] / baseline['measured_ach'] - 1) * 100:+.2f}%"
                   if baseline and baseline is not r else "(baseline)")
            line = (f"  maxCo={r['max_co']:<4} measured ACH={r['measured_ach']:8.4g}/hr  "
                    f"vs maxCo={MAXCO_VALUES[0]}: {pct:>10}   wall-clock={r['wall_clock_s']:7.1f}s")
        print(line)
        summary_lines.append(line)

    with open(RESULTS_PATH, "w") as f:
        json.dump({"base_build_s": cached["base_build_s"], "control_end_time_s": cached["control_end_time"],
                   "results": results}, f, indent=2)

    with open(NOTES_PATH, "a") as f:
        f.write(f"\nFinished: {datetime.now().isoformat(timespec='seconds')}\n\n```\n")
        f.write("\n".join(summary_lines))
        f.write("\n```\n\n**How to read this**: `measured ACH` is the actual ventilation air-change "
                f"rate the CFD run's own decay curve implies (fit via decay_analysis."
                f"fit_effective_decay_rate), not the nominal {ACH}/hr input - the two SHOULD agree "
                "closely if maxCo isn't biasing the result. The `%` column compares each maxCo "
                f"against the maxCo={MAXCO_VALUES[0]} baseline (the conservative default): if it "
                "stays small (well inside noise/fit uncertainty) up through maxCo=10, that's real "
                "evidence today's production setting isn't trading away accuracy for speed. A clear "
                "trend that grows with maxCo (especially by maxCo=15) confirms there IS a real "
                "accuracy cost, and pins down roughly where it starts to bite.\n")
    print(f"\nAppended results to {NOTES_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build-base", action="store_true")
    group.add_argument("--only-maxco", type=int)
    group.add_argument("--assemble", action="store_true")
    args = parser.parse_args()

    if args.build_base:
        build_base()
    elif args.only_maxco is not None:
        run_leg(args.only_maxco)
    elif args.assemble:
        assemble()
