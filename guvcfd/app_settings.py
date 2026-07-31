"""Advanced/expert tunables that apply across every project (unlike
run_settings.json, which is per-.guvcfd-project) - persisted in a single
fixed-name JSON file at the GUV-CFD repo root, edited via the Settings
menu (right of File) and auto-reloaded fresh at the start of every run,
so a change takes effect immediately without restarting the app.
"""
import json
from pathlib import Path

ADVANCED_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "advanced_settings.json"

# rel_tol values are stored as percentages (e.g. 1.0 = 1%) - what the
# Settings UI shows and edits - not the 0.0-1.0 fraction the pipeline
# functions themselves take; divide by 100 at the call site.
ADVANCED_SETTINGS_DEFAULTS = {
    "flow-rel-tol": 1.0,       # % - converge_flow_field's rel_tol
    "flow-max-iterations": 20000,  # hard cap on total flow-convergence iterations
    "plateau-rel-tol": 1.0,    # % - steady-state phase plateau CV threshold (same window as T_ss)
    "pimple-delta-t": 0.5,     # seconds - decay solver time step
    "mesh-cell-size": 0.10,    # meters
    "uv-zone-bins": 25,        # bins
    "momentum-relaxation": 0.7,  # SIMPLE under-relaxation for U/(k|omega)
    "scalar-relaxation": 0.7,    # SIMPLE under-relaxation for T
    # scalarTransport1 (controlDict) solves T OUTSIDE PIMPLE's own outer-
    # corrector loop, once per timestep by default - scalar-relaxation only
    # avoids biasing the result if nCorr/tolerance are high/tight enough for
    # this loop to actually iterate to convergence (confirmed empirically:
    # relax=1.0 with defaults and relax=0.7 with nCorr=3/tol=1e-4 converged
    # to the same answer; relax=0.7 with OpenFOAM's own defaults (nCorr=0,
    # tolerance=1) did not - see "OpenFoam settings background.md").
    "scalar-transport-ncorr": 3,        # scalarTransport1's own outer correctors
    "scalar-transport-tolerance": 1e-4,  # scalarTransport1's own initial-residual target
    # t-infinity-early-stop-enabled is now purely a speed optimization (chunked
    # early-exit if the T-infinity fit happens to stabilize quickly) - it no
    # longer gates Phase 1 acceptance on its own; see
    # phase1-require-stable-extrapolation below for that. Decoupled after a
    # real ACH=6 case (a genuinely, persistently oscillating flow field - see
    # ANALYSIS_LOG.md) repeatedly failed to ever produce a stable
    # extrapolation fit even though the underlying windowed-average
    # reduction_pct/eACH_uv were already good and stable - residence-time-
    # scaled deltaT (see compute_scaled_delta_t) substantially reduces the
    # odds the historical "false CV-plateau" failure this gate was built to
    # catch recurs, by fixing its root cause (insufficient real residence-
    # time coverage) rather than requiring a fragile curve fit on top.
    "t-infinity-early-stop-enabled": True,
    "t-infinity-rel-tol": 2.0,   # % - T-infinity stability tolerance (see check_t_infinity_stability)
    # The actual Phase 1 acceptance gate (was tied to t-infinity-early-stop-
    # enabled above) - off by default. Sweep mode has NO resume UX for a
    # Phase1ExtrapolationUndecided pause at all (a stuck combo just fails,
    # see scenario_runs.run_sweep's own docstring) - confirmed as a real,
    # blocking failure on a live 2-combination sweep, not just an interactive-
    # UI inconvenience. Turn on only if you specifically want the stricter,
    # curve-fit-verified eACH_uv figure and are prepared to babysit a run
    # that may need to pause for a decision (single-run mode only).
    "phase1-require-stable-extrapolation": False,
    # Phase 1/2's own chunk size (was hardcoded 500) and write cadence
    # (was hardcoded 200 for Phase 1 / 100 for Phase 2, unified here) - see
    # _run_phase/_chunk_write_interval in steady_state_pipeline.py. Shorter
    # chunks catch T-infinity convergence earlier and lose less progress if
    # the run is interrupted (app restart, crash, reboot); longer chunks
    # have less per-chunk overhead (fresh solver launch, mesh re-read,
    # postProcessing, field copy-back) and a more stable T-infinity refit.
    "phase-chunk-size": 400,       # iterations
    "phase-write-interval": 200,   # iterations
    "keep-all-timesteps": False,  # opt-in - see steady_state_pipeline.run_steady_state_scenario
    # Residence-time-scaled deltaT (steady_state_pipeline.compute_scaled_delta_t)
    # - default production mode as of 2026-07-29. Lets each phase's existing
    # phaseN-iterations budget cover the paper-cited "4-6 residence times" a
    # well-mixed room's C(t) needs to approach steady state, by scaling
    # OpenFOAM's own pseudo-time step instead of running more iterations -
    # confirmed directly on 3 real cases (ACH=3/6/9) to match a ~2.5-4x
    # larger deltaT=1 budget's reduction_pct/eACH_uv almost exactly. Purely
    # additive on top of the existing _settling_iterations-based iteration
    # floor/safety-multiplier/ceiling (app.py/scenario_runs.py) - deltaT only
    # ends up above 1 when that iteration budget still isn't enough to cover
    # the target residence-time span, so a case that already converges fine
    # at deltaT=1 is unaffected. Disabled automatically whenever
    # keep-all-timesteps is on (the two aren't compatible - see _run_phase).
    "deltat-scaling-enabled": True,
    "deltat-effective-fraction": 0.7,  # measured ACH/eACH_uv usually runs below nominal - conservative derating
    "deltat-target-fraction": 0.995,  # matches _settling_iterations' own default - ~5.3 residence times
    "oscillation-window": 6,        # chunks - run_pipeline._is_stable_oscillation
    "oscillation-growth-tol": 1.5,  # ratio - run_pipeline._is_stable_oscillation
    "ach-delivery-tol": 10.0,   # % - run_pipeline.check_ach_delivery
    "mass-balance-tol": 10.0,   # % - contaminant_source.windowed_mass_balance (steady-state Phase 1 cross-check)
    "phase1-t-initial": 0.0,    # Phase 1's starting T value - see run_steady_state_scenario
    "phase1-extrapolation-streak": 3,  # consecutive stable T-infinity fits required (see check_t_infinity_stability)
    "phase1-settling-safety-multiplier": 2.5,  # safety factor on the ACH-based minimum iteration estimate
    "phase1-max-iterations-ceiling": 40000,  # hard backstop - see Phase1ExtrapolationUndecided
    "decay-ach-min-fraction": 90.0,   # % - decay-mode UV-off control run's target reduction
    "decay-each-min-fraction": 90.0,  # % - decay-mode UV-on run's baseline target reduction
    "decay-each-max-fraction": 99.9,  # % - decay-mode UV-on run's target when eACH is high (cheap to reach)
    "keep-shared-scratch-dirs": False,  # troubleshooting opt-in - see scenario_runs.py's cleanup_ach_fn
}


def load_advanced_settings():
    """The saved advanced settings, backfilling any field missing from an
    older/partial file (or a missing file entirely) with its default -
    never raises, and the returned dict always has every key.
    """
    saved = {}
    if ADVANCED_SETTINGS_PATH.exists():
        try:
            with open(ADVANCED_SETTINGS_PATH) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            saved = {}
    return {k: saved.get(k, default) for k, default in ADVANCED_SETTINGS_DEFAULTS.items()}


def save_advanced_settings(settings):
    """Writes exactly the known keys (ignores anything extra the caller
    passed) so a stray/renamed field can never get permanently baked in.
    """
    to_save = {k: settings.get(k, default) for k, default in ADVANCED_SETTINGS_DEFAULTS.items()}
    with open(ADVANCED_SETTINGS_PATH, "w") as f:
        json.dump(to_save, f, indent=2)
    return to_save
