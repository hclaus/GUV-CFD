"""One-off validation (not wired into the GUI): does a genuinely transient
pimpleFoam run - real physical time, source AND UV both active from a cold
start, flow field (U/p/k/omega) allowed to evolve/oscillate freely rather
than staying frozen - reach the same room-average steady-state T as the
existing SIMPLE-based Phase 2 result for the same case?

Motivation (see MEMORY project_each_uv_decay_vs_steadystate.md and
v10_v11_decay_vs_steadystate_comparison.csv): decay mode and steady-state
mode disagree on eACH_uv by ~1.8-2.6x across every Z/ACH combo tried so far,
with no confirmed explanation - 4 hypotheses already investigated and
rejected with real CFD data. Steady-state's Phase 1/2 both run under
simpleFoam (SIMPLE), which has no ddt() term at all for U/p/k/omega (pure
pseudo-time relaxation) - so once the flow field is accepted (including via
the "bounded oscillation" path converge_flow_field() uses for cases like
this one, Z=6/ACH=6, which never reached a strict residual-based verdict),
it stays FROZEN at whatever phase of its oscillation it happened to be
accepted at, and only T is marched forward against that one frozen
snapshot. This script tests whether that frozen-flow-field assumption is
biasing Phase 2's T_ss - if a real transient (flow genuinely evolving)
lands on the same T_ss, the SIMPLE/PIMPLE solver choice isn't the
explanation; if it lands substantially differently (especially if closer
to decay mode's own answer), that's a real, novel finding worth chasing.

Reuses the already-converged, already-run Z6_ACH6 case directory from the
patient_ward_4B1_v11_simsstate project (steady-state mode) - its
constant/fvOptions already holds Phase 2's exact configuration (contaminant
source + all UV zones active, frozen there since that was the last phase
that ran). Clones it, resets T to a cold start (0 - same convention Phase 1
itself already uses, see steady_state_pipeline's phase1-t-initial setting),
and runs pimpleFoam directly against the SAME mesh/converged-flow-field/
fvOptions, letting U/p/k/omega evolve for real this time.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guvcfd.decay_analysis import check_plateau_windowed, read_vol_average_dat, windowed_stats
from guvcfd.initial_fields import restore_boundary_conditions, compute_inlet_velocities
from guvcfd.splice import set_control_dict_start_from, set_control_dict_time, set_function_write_interval
from guvcfd.steady_state_pipeline import compute_corrected_eACH_uv_from_control
from guvcfd.wsl_utils import run_wsl_or_raise, run_wsl_streaming, wsl_path

PROJECT_DIR = r"\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\patient_ward_4B1_v11_simsstate"
SOURCE_CASE = f"{PROJECT_DIR}/Z6_ACH6"
TEST_CASE = f"{PROJECT_DIR}/_pimple_validation_Z6_ACH6"

# run_settings.json / results.json for SOURCE_CASE (already on disk - see
# patient_ward_4B1_v11_simsstate_summary.csv's Z=6/ACH=6 row). room_volume
# is the exact value from results.json ("room_volume": 39.475199999999994) -
# only the product matters for compute_inlet_velocities, so use it directly
# rather than guessing individual x/y/z dimensions.
ACH = 6.0
ROOM_VOLUME = 39.475199999999994
INLET_WALL = "xMin"
INLET_SIZE = (0.4, 0.4)
VENTILATION_ACH_MEASURED = 3.7491764645322685  # this combo's own control-run result (already on disk)
PHASE2_T_SS_WINDOWED = 0.02826758917777778
PHASE2_T_SS_TINF = 0.027631190451271624

CHUNK_SECONDS = 300
MAX_SECONDS = 3600
WINDOW_FRAC = 0.15
PLATEAU_REL_TOL = 0.01


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    source_wsl = wsl_path(SOURCE_CASE)
    test_wsl = wsl_path(TEST_CASE)
    parent_wsl = wsl_path(PROJECT_DIR)

    log(f"Cloning {SOURCE_CASE} -> {TEST_CASE} ...")
    run_wsl_or_raise(f'rm -rf "{test_wsl}" && cp -r "{source_wsl}" "{test_wsl}"',
                      parent_wsl, "cloning case for PIMPLE validation")

    log("Removing every non-0 time directory and stale postProcessing "
        "(starting a fresh transient from t=0, not continuing Phase 2's SIMPLE run)...")
    run_wsl_or_raise(
        'bash -c \'for d in [1-9]*/; do rm -rf "$d"; done\' && rm -rf postProcessing log.pimpleFoam',
        test_wsl, "removing stale time directories/postProcessing")

    log("Resetting T to a cold start (uniform 0, same convention Phase 1 already uses); "
        "U/p/k/omega/nut internalField left untouched (the converged flow field)...")
    room_volume = ROOM_VOLUME
    velocities = compute_inlet_velocities(ACH, room_volume, [(INLET_WALL, INLET_SIZE[0] * INLET_SIZE[1])])
    restore_boundary_conditions(TEST_CASE, inlet_velocity=velocities[0], T_initial=0)

    log("Setting controlDict for a fresh transient pimpleFoam run...")
    set_control_dict_start_from(TEST_CASE, "startTime")
    set_control_dict_time(TEST_CASE, end_time=CHUNK_SECONDS, write_interval=CHUNK_SECONDS, delta_t=0.5)
    # set_control_dict_time's writeInterval sweep above also clobbers
    # volAverageLive1's own nested writeInterval (needed so scalarTransport1's
    # T gets written on the same schedule as U/p/k/omega) - re-pin it to 1
    # (every timestep) so windowed_stats has a dense series to work with,
    # same idiom steady_state_pipeline._run_phase already uses.
    set_function_write_interval(TEST_CASE, "volAverageLive1", 1)

    elapsed = 0
    all_t, all_T = [], []
    plateaued = False
    while elapsed < MAX_SECONDS:
        log(f"=== Running pimpleFoam: {elapsed}-{elapsed + CHUNK_SECONDS}s of up to {MAX_SECONDS}s ===")
        r = run_wsl_streaming("pimpleFoam 2>&1 | tee -a log.pimpleFoam", test_wsl,
                               on_line=None, kill_pattern="pimpleFoam")
        if r.returncode != 0 or "FOAM FATAL" in (r.stdout or "") or "Floating Point Exception" in (r.stdout or ""):
            tail = "\n".join((r.stdout or "").splitlines()[-40:])
            raise RuntimeError(f"pimpleFoam failed (exit {r.returncode}):\n{tail}")
        elapsed += CHUNK_SECONDS

        chunk_t, chunk_T = read_vol_average_dat(
            f"{TEST_CASE}/postProcessing/volAverageLive1/{elapsed - CHUNK_SECONDS}/volFieldValue.dat")
        all_t.extend(chunk_t.tolist())
        all_T.extend(chunk_T.tolist())

        with open("pimple_validation_progress.json", "w") as f:
            json.dump({"t": all_t, "T": all_T, "elapsed": elapsed}, f)

        if len(all_T) < 10:
            log(f"  t={all_t[-1]:.1f}s  only {len(all_T)} samples so far - too few for a windowed stat yet")
            set_control_dict_start_from(TEST_CASE, "latestTime")
            set_control_dict_time(TEST_CASE, end_time=elapsed + CHUNK_SECONDS)
            set_function_write_interval(TEST_CASE, "volAverageLive1", 1)
            continue

        mean, std, cv, n, window_span = windowed_stats(all_t, all_T, frac=WINDOW_FRAC)
        converged, plateau_cv = check_plateau_windowed(all_t, all_T, frac=WINDOW_FRAC, rel_tol=PLATEAU_REL_TOL)
        log(f"  t={all_t[-1]:.1f}s  n={len(all_T)}  windowed T_ss={mean:.6f}  CV={(cv or 0) * 100:.2f}%  plateaued={converged}")

        if converged:
            plateaued = True
            break

        set_control_dict_start_from(TEST_CASE, "latestTime")
        set_control_dict_time(TEST_CASE, end_time=elapsed + CHUNK_SECONDS)
        set_function_write_interval(TEST_CASE, "volAverageLive1", 1)

    mean, std, cv, n, window_span = windowed_stats(all_t, all_T, frac=WINDOW_FRAC)
    T_ss_pimple = mean

    eACH_uv_pimple = compute_corrected_eACH_uv_from_control(
        T_ss_pimple, Su=0.3084, source_volume=0.064, room_volume=room_volume,
        ventilation_ach_measured=VENTILATION_ACH_MEASURED)

    result = {
        "elapsed_seconds": elapsed,
        "plateaued": plateaued,
        "T_ss_pimple_windowed": T_ss_pimple,
        "T_ss_pimple_cv_pct": (cv or 0) * 100,
        "eACH_uv_pimple_corrected": eACH_uv_pimple,
        "phase2_T_ss_windowed_simple": PHASE2_T_SS_WINDOWED,
        "phase2_T_ss_tinf_simple": PHASE2_T_SS_TINF,
        "eACH_uv_steady_corrected_simple": 61.39459659057191,
        "eACH_uv_decay_corrected": 27.259894916136286,
        "ratio_pimple_over_simple": T_ss_pimple / PHASE2_T_SS_WINDOWED if T_ss_pimple else None,
    }
    with open("pimple_validation_result.json", "w") as f:
        json.dump(result, f, indent=2)
    log("=== RESULT ===")
    log(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
