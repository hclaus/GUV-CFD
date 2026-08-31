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
    # Lowered from 0.7 to 0.5 on 2026-08-19 after a confirmed real case: at
    # 0.1m mesh, 0.7 let U/(k|omega) genuinely fail to converge (Phase 1/2
    # oscillating indefinitely, T_ss_cv ~2-3%, never settling even across
    # the full iteration budget) - 0.5 alone fixed it, converging cleanly
    # (T_ss_cv ~0.05%) to the SAME T_ss finer meshes independently agreed
    # on, confirming it was a convergence-path issue, not a different
    # physical answer at that mesh. A same-mesh diagnostic swapping
    # div(phi,U) to plain "upwind" instead ALSO stopped the oscillation but
    # converged to a different (wrong) T_ss - ruled out as the fix; 0.5 was
    # adopted as the new default instead of a scheme change specifically
    # because it doesn't alter the physics being solved, only how
    # carefully the iteration approaches it.
    "momentum-relaxation": 0.5,  # SIMPLE under-relaxation for U/(k|omega)
    "scalar-relaxation": 0.7,    # SIMPLE under-relaxation for T
    # Off by default (opt-in) - when on, scalar-relaxation above is ignored
    # and T's relaxation is instead computed per-case from its own
    # kUV.max (see splice.compute_adaptive_scalar_relaxation), fit against a
    # real calibration campaign (2026-08-24/25) showing a fixed relaxation
    # that's safe for a low-Z lamp design can crash Phase 2 outright on a
    # high-Z one (T growing ~500x per outer iteration, up to ~1e80 before
    # the crash is caught) - and, conversely, that a fixed low relaxation
    # safe enough for the worst case wastes iterations on every easier one.
    "adaptive-t-relaxation": False,
    # Off by default (opt-in) - independent of adaptive-t-relaxation above
    # (that tunes how the outer loop APPROACHES a diverging cell; this
    # catches the divergence itself if it happens anyway). When on, a
    # custom OpenFOAM function object (tclamp_decay.py) watches Phase 2's
    # T field every outer iteration and, for any cell outside [0, Tmax],
    # replaces it with a locally sink-decayed value (T*exp(-kUV*dt), then
    # clamped) rather than a hard reset - the pure-sink ODE's own analytic
    # solution, so the correction stays physically motivated instead of an
    # arbitrary snap to a boundary. Tmax itself is set per-run as
    # t-clamp-decay-multiplier times Phase 1's own converged source-zone
    # max T (see tclamp_decay.source_zone_max_T) - the only physically
    # meaningful reference this pipeline has for "how concentrated does
    # the source actually get". Added 2026-08-27 after a real, confirmed
    # divergence mechanism (see ANALYSIS_LOG.md): a single cell's outer-
    # iteration T can blow up past 1e100 or go negative within ~20
    # iterations at high local kUV, even with adaptive relaxation already
    # at its floor.
    "t-clamp-decay-enabled": False,
    "t-clamp-decay-multiplier": 1.3,
    # Experimental (2026-08-30) - off by default. Adds airflow to the source
    # cellZone so the contaminant the volumetric T source injects there is
    # carried by moving air (~0.06 m/s, resting tidal breathing) instead of
    # appearing in still air. Implemented as a CONSTRAINT on U in that zone
    # (vectorFixedValueConstraint), not a momentum source: a source only adds
    # terms to the momentum equation and gets overruled by SIMPLE's pressure
    # correction - measured converging to 2.25 m/s against a 0.06 m/s target
    # (37x) even with correct coefficients. See
    # contaminant_source.breathing_inlet_velocity_constraint's docstring and
    # ANALYSIS_LOG.md's 2026-08-30 entry. Still UNVALIDATED: nobody has yet
    # confirmed on a real run that zone |U| reads 0.06 and that mass balance
    # is unharmed.
    "breathing-inlet-enabled": False,
    # Direction the exhale is blown, as a vector (normalised at use, so only
    # the ratio matters - the 0.06 m/s magnitude is applied separately;
    # all-zero or an unreadable value falls back to the default). NOT a
    # cosmetic default: with the source at
    # (0.4, 1.2, 1.3) in patient ward 4B1 v7 the +x default pointed straight
    # into the 'outlet' patch (xMax wall, same y and z), short-circuiting two
    # thirds of the contaminant into the extract before it mixed and dragging
    # Phase 1's T_ss1 from ~1.13 down to 0.42. Defaults to (0,0,1) - straight
    # up - because a vertical jet cannot line up with a wall-mounted opening,
    # so it is the safe choice when occupant orientation is unknown. A
    # horizontal vector models a directed exhale (breath leaves the mouth
    # roughly level, in the direction faced); set it from the occupant's
    # actual orientation - see ANALYSIS_LOG.md 2026-08-31.
    "breathing-inlet-dir-x": 0.0,
    "breathing-inlet-dir-y": 0.0,
    "breathing-inlet-dir-z": 1.0,
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
    # Default flipped True->True is intentional (2026-08-10): a later sweep
    # launch on the same project_dir now validates reuse by flow_fingerprint
    # (see project_status.find_reusable_ach_base) rather than blind file
    # presence, so keeping these around by default is safe and is what
    # makes "apply a different UV design"/"add more Z/ACH" fast across
    # separate launches, not just within one - see the "Extend / modify
    # simulations" modal's Clean up shared scratch directories action for
    # the explicit disk-space release valve this trades for.
    "keep-shared-scratch-dirs": True,
    # pimpleFoam's adaptive-timestep Courant cap (splice.set_control_dict_time's
    # own _MAX_CO=5 was the hardcoded value before this) - was a per-project-
    # only setting with zero UI anywhere (see PROJECT_OPENFOAM_SETTINGS_KEYS
    # below), only ever changeable by hand-editing a .guvcfd file directly.
    # Promoted to a normal advanced default 2026-08-07, then raised to 10 the
    # same day once a dedicated A/B decay-accuracy test (compare_maxco_decay_
    # accuracy.py, see maxco_decay_accuracy_NOTES.md) confirmed 10 vs. the
    # original 5 costs only ~2.5% on the measured decay rate for a real ~36%
    # wall-clock speedup - well under this project's ~200% eACH materiality
    # bar. 15 was also tested (~5% cost, ~55% speedup - diminishing returns,
    # not adopted as the default). Lower this project-by-project if a
    # specific case's own flow/mesh needs more numerical margin.
    "max-co": 10,
    # scenario_runs._MAX_CONCURRENT_SOLVES's own tunable ceiling - how many
    # OpenFOAM processes (any stage: flow convergence, Phase 1, control,
    # Phase 2/decay) a sweep runs at once. Was a hardcoded 9, sized purely
    # by CPU core headroom with no memory awareness at all. Lowered to 5
    # as the default after a real, confirmed overnight sweep failure
    # (2026-08-20): a full 25-combo sweep at 9 concurrent solves killed
    # itself in 5 separate waves over ~4h45m - one from an actual WSL VM
    # crash, the other four from processes being killed mid-solve (healthy,
    # converging normally right up to an abrupt cutoff) with no trace in
    # WSL's own kernel log, consistent with resource contention even with
    # WSL's memory ceiling already raised to 10GB (see "Linux
    # installation.md"). Raise this back up project-by-project only on a
    # machine confirmed to have the RAM for it - more concurrency is pure
    # wall-clock speed, less is reliability, and this project's own sweep
    # runs are unsupervised overnight, where reliability wins.
    "max-concurrent-solves": 5,
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


# Every OpenFOAM/meshing/solver setting that should be captured per-project
# (into the .guvcfd file, at save time - see app.py's _capture_openfoam_settings/
# qtapp's equivalent) rather than only living in the global advanced_settings.json -
# this is what makes a specific run's exact settings reproducible regardless
# of what the *global* advanced settings later change to, and lets a copied/
# edited .guvcfd experiment with one setting while pinning everything else.
# Excludes deltat-scaling-enabled/-effective-fraction/-target-fraction
# (already migrated earlier - see steady_state_pipeline.merge_project_deltat_settings,
# the precedent this generalizes) and app-behavior-only toggles with no
# effect on simulation results (keep-all-timesteps, keep-shared-scratch-dirs).
PROJECT_OPENFOAM_SETTINGS_KEYS = (
    "flow-rel-tol", "flow-max-iterations", "plateau-rel-tol", "pimple-delta-t",
    "mesh-cell-size", "uv-zone-bins",
    "momentum-relaxation", "scalar-relaxation", "adaptive-t-relaxation",
    "t-clamp-decay-enabled", "t-clamp-decay-multiplier",
    "breathing-inlet-enabled",
    "breathing-inlet-dir-x", "breathing-inlet-dir-y", "breathing-inlet-dir-z",
    "scalar-transport-ncorr", "scalar-transport-tolerance",
    "t-infinity-early-stop-enabled", "t-infinity-rel-tol",
    "phase1-require-stable-extrapolation", "phase-chunk-size", "phase-write-interval",
    "oscillation-window", "oscillation-growth-tol",
    "ach-delivery-tol", "mass-balance-tol",
    "phase1-t-initial", "phase1-extrapolation-streak",
    "phase1-settling-safety-multiplier", "phase1-max-iterations-ceiling",
    "decay-ach-min-fraction", "decay-each-min-fraction", "decay-each-max-fraction",
    "max-co", "max-concurrent-solves",
)


def _resolved_default(key, adv):
    """key's value from adv if present, else ADVANCED_SETTINGS_DEFAULTS -
    never raises. `adv` is a complete dict in production
    (load_advanced_settings()'s own contract), but tests commonly
    monkeypatch it with a partial dict for brevity - falling back to the
    real module-level default keeps this function's own "never raises,
    always resolves to something sensible" contract regardless of how
    complete `adv` is, matching load_advanced_settings() itself.
    """
    return adv[key] if key in adv else ADVANCED_SETTINGS_DEFAULTS[key]


def merge_project_openfoam_settings(settings, adv):
    """Like steady_state_pipeline.merge_project_deltat_settings, but for
    every other key in PROJECT_OPENFOAM_SETTINGS_KEYS. Call once right
    after load_advanced_settings() - every existing adv["mesh-cell-size"]-
    style call site downstream then picks up the per-project override
    automatically, with no change needed at that call site. Falls back to
    adv's global-default value for a project saved before this feature
    existed.
    """
    merged = dict(adv)
    for key in PROJECT_OPENFOAM_SETTINGS_KEYS:
        merged[key] = settings.get(key, _resolved_default(key, adv))
    return merged


def capture_openfoam_settings(settings, adv):
    """Fill in any PROJECT_OPENFOAM_SETTINGS_KEYS missing from `settings`
    (a .guvcfd project dict about to be saved) with their CURRENT resolved
    value from `adv` - mutates and returns `settings`. Called at save time, not at run time:
    once a key is present, it's never overwritten by a later save (that
    would defeat the point - a project's pinned settings must survive the
    global advanced settings changing later), so this only ever backfills
    a brand new project or one saved before this feature existed.
    """
    for key in PROJECT_OPENFOAM_SETTINGS_KEYS:
        if key not in settings:
            settings[key] = _resolved_default(key, adv)
    return settings
