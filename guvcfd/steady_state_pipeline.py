"""Two-phase steady-state (continuous source) scenario, formalized from the
manual steps verified against roomVent_scalar_uv_ill and _ill2-SS: a
continuous contaminant source reaches equilibrium with ventilation alone
(phase 1), then UV cellZones are added on top of the still-active source
and a new, lower equilibrium is reached (phase 2).

Assumes the case has already been through run_pipeline.setup_case() (or
equivalent): mesh with inlet/outlet, converged flow field, fluenceRate/kUV
computed, and cellZones/fvOptions containing the UV sink zones. This
pipeline only adds the source cellZone and orchestrates the two phases -
it doesn't redo mesh generation or flow convergence.
"""
import json
import math
import re
from pathlib import Path

import numpy as np

from .case_io import read_openfoam_scalar_field
from .cellzones import bin_decay_rates
from .contaminant_source import (
    write_source_topo_set_dict, compute_source_strength, source_Su, source_fvoptions_entry,
    write_fvoptions_file, live_mass_balance_functions, windowed_mass_balance,
)
from .decay_analysis import (
    read_vol_average_dat, check_plateau_windowed, windowed_stats,
    windowed_stats_detrended, fit_asymptotic_value, check_t_infinity_stability,
)
from .initial_fields import restore_boundary_conditions, resolve_inlet_velocity
from .mesh_gen import opening_center, opening_half_extents
from .monitoring import write_vol_average_dict, live_vol_average_functions
from .monitoring_points import write_monitoring_topo_set_dict, zone_name
from .splice import (
    splice_fv_options_into_control_dict, splice_into_functions_block,
    set_control_dict_time, set_function_write_interval, ensure_simple_fvsolution,
    disable_simple_residual_control,
)
from .wsl_utils import wsl_path, run_wsl_or_raise, run_wsl_streaming, StoppedByUser


class Phase1ExtrapolationUndecided(Exception):
    """Raised when Phase 1's iteration ceiling is exhausted without
    fit_asymptotic_value ever reaching a stable, accepted extrapolation
    (decay_analysis.check_t_infinity_stability) - the primary readiness
    gate for Phase 1. A windowed CV-plateau check alone can be fooled by a
    curve that's still genuinely rising: confirmed directly on a real run
    where a CV=0.56% "plateaued" verdict at 18000 iterations sat on a curve
    that needed ~25000 iterations to actually reach 95% mass balance, while
    the SAME run's T-infinity extrapolation was already accurate (within
    0.22%) using only the original 18000-iteration data. Mirrors
    run_pipeline.FlowConvergenceUndecided's role and shape exactly - an
    expected outcome needing a human decision, not a crash, carrying a
    diagnostic (see _phase1_extrapolation_diagnostic) and enough state
    (see _write_phase1_pending/resume via run_steady_state_scenario's
    phase1_resume_decision) to continue without redoing mesh/flow work or
    restarting Phase 1's own iteration count from scratch.

    Distinct from a genuine solver failure (FOAM FATAL, non-zero exit,
    StoppedByUser) - those still raise RuntimeError/StoppedByUser as
    before.
    """

    def __init__(self, message, diagnostic, total_iterations):
        super().__init__(message)
        self.diagnostic = diagnostic
        self.total_iterations = total_iterations


def _phase1_extrapolation_diagnostic(tinf_history, streak, rel_tol, n_iterations, check_interval):
    """Best-effort analysis of Phase 1's T-infinity fit history when the
    iteration ceiling was reached without check_t_infinity_stability ever
    accepting a streak - mirrors run_pipeline._oscillation_diagnostic's
    role for flow convergence: distinguishes "never saw enough curvature
    to even attempt a fit" from "fits keep failing/disagreeing" from
    "fits agree, just not for a full streak yet" - these need different
    user-facing messages, not one generic failure.
    """
    n_attempts = len(tinf_history)
    n_successful = sum(1 for v in tinf_history if v is not None)
    recent = tinf_history[-streak:] if tinf_history else []
    recent_successful = [v for v in recent if v is not None]

    if n_successful == 0:
        summary = (
            f"No extrapolation could be fit yet across {n_attempts} check(s) - the curve hasn't "
            f"shown enough curvature toward an asymptote yet. This usually just means more "
            f"iterations are needed, not that something is wrong."
        )
    elif len(recent_successful) < streak:
        summary = (
            f"Extrapolation fits are succeeding ({n_successful}/{n_attempts} checks so far), but "
            f"not yet {streak} in a row without interruption - the most recent {len(recent)} "
            f"check(s) included at least one where the fit failed."
        )
    else:
        mean_est = sum(recent_successful) / len(recent_successful)
        spread = (max(recent_successful) - min(recent_successful)) / mean_est if mean_est else float("inf")
        summary = (
            f"The last {streak} extrapolation estimates ({[round(v, 4) for v in recent_successful]}) "
            f"are still {spread:.1%} apart (target <= {rel_tol:.0%}) - trending toward agreement, "
            f"just not stable enough yet."
        )

    return {
        "n_attempts": n_attempts, "n_successful_fits": n_successful,
        "recent_estimates": recent, "streak_required": streak, "rel_tol": rel_tol,
        "chunk_size": check_interval, "n_iterations": n_iterations,
        "summary": summary,
    }


def compute_corrected_eACH_uv_from_control(T_ss2, Su, source_volume, room_volume, ventilation_ach_measured):
    """Corrected eACH_uv using a ventilation rate measured the same way
    decay mode measures it - a dedicated UV-off control run (see
    scenario_runs._run_shared_control) - instead of deriving it from
    Phase 1's own point-source buildup (compute_corrected_eACH_uv).

    Phase 1 starts from T=0 and continuously injects from a single,
    typically tiny (see setup_case's source_size) source zone - the room-
    average concentration can only rise as fast as the contaminant
    physically spreads from that point, which is a transport-plus-removal
    problem, not the removal-only problem a decay-style control (uniform
    initial condition, no source, just relaxing via ventilation) measures.
    Confirmed directly on a real combo: Phase 1's own buildup curve was
    STILL visibly rising after 6500s/6400 iterations (its T-infinity
    extrapolation's own fitted time constant, ~2450s, was already slower
    than either of decay's own ventilation-rate estimates for the same
    room/ACH) - accepting that as "the" steady state underestimates T_ss1,
    which inflates both the derived ventilation rate and eACH_uv_steady_state
    (both use T_ss1 in a denominator/ratio). A control run sidesteps this
    entirely: it starts already-mixed, so it only ever measures removal,
    never mixing-transport lag.

    lambda_total_actual = G/(V*T_ss2) still comes from Phase 2 (unchanged,
    and not subject to the same problem - adding UV only shortens the
    system's time constant, so Phase 2 converges faster than Phase 1, not
    slower). eACH_uv = lambda_total_actual - ventilation_ach_measured,
    mirroring decay_analysis.compute_effective_eACH's identical
    "total minus measured ventilation" subtraction exactly.

    Returns eACH_uv_corrected, or None if T_ss2 isn't usable (zero/falsy).
    """
    if not T_ss2:
        return None
    G_total = Su * source_volume
    lambda_total_actual = G_total / (room_volume * T_ss2) * 3600
    return lambda_total_actual - ventilation_ach_measured


def compute_corrected_eACH_uv(T_ss1, T_ss2, Su, source_volume, room_volume):
    """Corrected eACH_uv using the *actual* ventilation removal rate instead
    of the nominal ACH - derived for free from Phase 1's own steady state,
    no separate UV-off control run needed (unlike the decay scenario).

    Prefer compute_corrected_eACH_uv_from_control when a control run is
    available (run_sweep always runs one - see _run_shared_control) - this
    T_ss1-based version is biased whenever Phase 1 hasn't fully escaped
    its point-source mixing-transport lag (see that function's docstring),
    which is common for a small/localized source zone. Kept for the
    single-run path, which doesn't run a control.

    G (the total room-wide injection rate) was calibrated as
    room_volume*lambda_vent_nominal*target_T_ss (see
    contaminant_source.compute_source_strength). Phase 1 (source + no UV)
    reaches a real steady state T_ss1 under whatever ventilation efficiency
    this mesh/flow field actually achieves - at that equilibrium,
    injection = removal, so:
        lambda_vent_actual = G / (room_volume * T_ss1)
                            = lambda_vent_nominal * (target_T_ss / T_ss1)

    Caveat: T_ss1 here is the room-AVERAGE steady-state concentration, so
    this formula implicitly assumes a well-mixed room (average concentration
    == outlet concentration). It is NOT a measurement of the inlet's
    delivered flow rate - that's fixed at lambda_vent_nominal by the
    boundary condition itself, independent of mixing. If the room mixes
    imperfectly (e.g. inlet/outlet short-circuiting on the same wall), the
    room average builds up higher than a well-mixed room would for the same
    true flow rate, so lambda_vent_actual reads *below* lambda_vent_nominal
    even though the actual delivered ACH hasn't changed. Treat this as a
    ventilation-effectiveness metric, not a flow-rate measurement.

    Returns (ventilation_ach_measured, eACH_uv_corrected), or (None, None)
    if T_ss1/T_ss2 aren't usable (zero/falsy).
    """
    if not T_ss1 or not T_ss2:
        return None, None
    G_total = Su * source_volume
    lambda_vent_actual = G_total / (room_volume * T_ss1)
    eACH_uv_corrected = lambda_vent_actual * (T_ss1 / T_ss2 - 1) * 3600
    return lambda_vent_actual * 3600, eACH_uv_corrected


def _uv_fvoptions_entries(k_values, nbins):
    """Recompute UV sink zone fvOptions entry text from an existing kUV
    field (matches whatever cellZones setup_case() already wrote to
    constant/polyMesh/cellZones - same deterministic binning, so this is
    safe to recompute without touching the mesh).
    """
    bin_idx, bin_repr = bin_decay_rates(k_values, nbins)
    entries = []
    for b in range(nbins + 1):
        k = bin_repr[b]
        if k <= 0:
            continue
        name = f"uvZone{b}"
        entries.append("\n".join([
            f"uvSource_{name}", "{", "    type            scalarSemiImplicitSource;",
            "    active          true;", "", "    scalarSemiImplicitSourceCoeffs", "    {",
            "        selectionMode   cellZone;", f"        cellZone        {name};",
            "        volumeMode      specific;", "", "        injectionRateSuSp", "        {",
            f"            T           (0 {-k:.6e});", "        }", "    }", "}", "",
        ]))
    return entries


def _clean_time_dirs(case_dir_wsl):
    run_wsl_or_raise(
        "for d in [0-9]*/; do [ \"$d\" = \"0/\" ] || rm -rf \"$d\"; done",
        case_dir_wsl, "cleaning time directories",
    )


def _rename_chunk_time_dirs(case_dir_wsl, offset, dir_names):
    """Rename EXACTLY `dir_names` (this chunk's own new write_interval
    snapshots - see _run_phase, which computes this as the directory-
    listing diff from immediately before to immediately after the chunk's
    own simpleFoam invocation) to their true cumulative iteration count
    (name + offset), instead of deleting them - used when the user opts
    into keeping every intermediate snapshot (Settings: "keep all time
    steps") for ParaView playback.

    Previously this renamed every "[0-9]*/" directory found on disk via a
    single shell glob, regardless of whether THIS chunk created it. With
    keep_all_timesteps=True (which deliberately never cleans old
    directories between chunks), that silently re-renamed already-
    correctly-renamed directories from EARLIER chunks on every subsequent
    chunk, compounding their offsets on top of each other - confirmed on a
    real run: directory names inflated to 160,000+ despite the run only
    ever reaching ~12,700 iterations - and could even nest one directory
    inside another when a rename target happened to already exist (mv's
    own behavior for an existing directory destination), corrupting the
    case directory badly enough to crash a later step expecting a flat,
    correctly-named time directory.

    Passing the exact set of names this chunk itself created sidesteps
    both failure modes: each chunk's own true cumulative range
    [chunk_start+1, chunk_start+chunk_size] is disjoint from every other
    chunk's by construction (chunk_start only ever advances by chunk_size),
    so a rename target here can never collide with an existing directory,
    regardless of how many earlier chunks were kept around.
    """
    if offset == 0 or not dir_names:
        return
    names = " ".join(sorted(dir_names, key=int))
    cmd = f'for d in {names}; do mv "$d" "$((d + {offset}))"; done'
    run_wsl_or_raise(
        cmd, case_dir_wsl, "renaming this chunk's time directories to cumulative iteration counts",
    )


def _list_time_dirs(case_dir_wsl):
    """The current set of numbered time directory names (excluding "0"),
    as plain strings - the before/after snapshots _run_phase diffs to find
    exactly which directories a chunk's own simpleFoam invocation created.
    """
    r = run_wsl_or_raise(
        'ls -d [0-9]*/ 2>/dev/null | sed \'s#/##\' | grep -v "^0$" || true',
        case_dir_wsl, "listing existing time directories",
    )
    return set(r.stdout.split())


def _chunk_write_interval(write_interval, chunk_size):
    """write_interval must EVENLY DIVIDE this chunk's own duration, or no
    snapshot ever lands at the chunk's true end - not just when it's
    shorter than write_interval (the case this originally handled), but
    whenever chunk_size isn't itself a clean multiple of write_interval.

    controlDict's writeControl is "adjustableRunTime" (set once, in the
    template - set_control_dict_time() only ever rewrites endTime/
    writeInterval/deltaT's *values*, never writeControl itself) - unlike
    "timeStep" mode, this does not force a write at endTime. Confirmed as
    a real, silent ~20%-of-chunk compute waste on a live run: the
    T-infinity early-stop's hardcoded 500-iteration chunks against a
    200-iteration write_interval (500 not a multiple of 200) wrote
    snapshots at 200/400 but never at 500 - the solver genuinely ran all
    500 iterations, but iterations 401-500 were never written to disk, so
    _run_phase's "latest" (the true, trusted checkpoint) fell back to 400
    every single chunk, discarding the last 100 iterations' real progress
    each time. Returns the LARGEST divisor of chunk_size that's <=
    write_interval, so a write always lands exactly at chunk_size (the
    common case - chunk_size already a multiple of write_interval - is
    unaffected, this only ever narrows the interval when needed).
    """
    limit = min(write_interval, chunk_size)
    for candidate in range(limit, 0, -1):
        if chunk_size % candidate == 0:
            return candidate
    return 1  # unreachable (candidate=1 always divides chunk_size), kept as an explicit floor


def compute_scaled_delta_t(effective_rate_per_hr, n_iterations, target_fraction=0.995):
    """The residence-time-scaled deltaT a phase needs to reach
    target_fraction of its true steady state within a FIXED n_iterations
    budget, given the phase's effective removal rate (ACH for Phase 1,
    ACH+eACH_uv for Phase 2 - see _run_phase's own delta_t docstring for
    why this works: simpleFoam's flow solve has no ddt() term at all, but
    scalarTransport1's T equation does, and is solved implicitly, so
    deltaT can be scaled up freely without any stability penalty).

    Same "how close to steady state" criterion _settling_iterations()
    already uses (target_fraction=0.995 by default matches its own
    default) - this is the residence-time-based alternative to that
    function's "just run more iterations" approach: target_fraction=0.995
    implies ln(1/(1-0.995)) = ~5.3 residence times, squarely inside the
    "4-6 residence times" criterion the paper this was validated against
    states for a well-mixed room's C(t) to approach its steady state (see
    "OpenFOAM settings background.md").

    Returns max(1, round(...)) - never less than 1 (OpenFOAM's own
    historical default), so a case whose configured n_iterations budget
    already comfortably covers the needed residence-time span (e.g. a
    high-ACH case, where residence time is short) is left completely
    unaffected rather than slowed down. effective_rate_per_hr <= 0 (not
    physically meaningful - would need infinite time) also returns 1,
    leaving n_iterations as the only thing that can help (same fallback
    _settling_iterations uses for lambda_per_hr <= 0).

    Confirmed directly (three real cases: ACH=3/Z=1.7, ACH=6/Z=6,
    ACH=9/Z=6): a 1500/1000-iteration run using this scaling matched a
    4000/2500-iteration deltaT=1 run's reduction_pct/eACH_uv almost
    exactly, where an unscaled 1500/1000 run badly undershot both.
    """
    if effective_rate_per_hr <= 0:
        return 1
    theta_s = 3600.0 / effective_rate_per_hr
    target_cycles = math.log(1.0 / (1.0 - target_fraction))
    ideal_dt = target_cycles * theta_s / n_iterations
    return max(1, round(ideal_dt))


def resolve_phase_delta_ts(ach, eACH_uv_well_mixed, phase1_iterations, phase2_iterations, adv):
    """The phase1_delta_t/phase2_delta_t run_steady_state_scenario expects,
    from a project's ach/eACH_uv_well_mixed and its (already-resolved,
    possibly _settling_iterations-inflated) iteration budgets - the one
    place this is computed, shared by app.py's single-run path and
    scenario_runs.py's sweep paths, so the deltat-* advanced settings mean
    the same thing everywhere.

    Returns (1, 1) - i.e. the historical deltaT=1 behavior - whenever
    disabled via "deltat-scaling-enabled", or whenever keep-all-timesteps
    is on (the two aren't supported together, see _run_phase).
    """
    if not adv["deltat-scaling-enabled"] or adv["keep-all-timesteps"]:
        return 1, 1
    frac = adv["deltat-effective-fraction"]
    target_fraction = adv["deltat-target-fraction"]
    phase1_delta_t = compute_scaled_delta_t(frac * ach, phase1_iterations, target_fraction=target_fraction)
    phase2_delta_t = compute_scaled_delta_t(frac * (ach + eACH_uv_well_mixed), phase2_iterations,
                                             target_fraction=target_fraction)
    return phase1_delta_t, phase2_delta_t


def _copy_latest_to_zero(case_dir_wsl, latest, include_T, log_fn):
    fields = "U p k omega nut phi" + (" T" if include_T else "")
    r = run_wsl_or_raise(f"ls {latest}/", case_dir_wsl, "listing converged fields")
    available = set(r.stdout.split())
    to_copy = [f for f in fields.split() if f in available]
    log_fn(f"  Copying fields from {latest}/ to 0/: {to_copy}")
    cp_targets = " ".join(f"{latest}/{f}" for f in to_copy)
    run_wsl_or_raise(f"cp -f {cp_targets} 0/", case_dir_wsl, "copying converged fields")


_TIME_LINE_RE = re.compile(r"^Time\s*=\s*[\d.]+\s*$")


def _phase_solver_callback(log_fn, solver_log_fn, status_fn, status_key):
    """Wraps a phase's simpleFoam on_line callback.

    With no status_fn (single-run mode - progress there comes from
    solver_log_fn/app._track_solver_time instead, which has never shown
    per-iteration lines in its own log), this is a no-op and returns
    exactly what the call site used before status_fn existed
    (solver_log_fn or log_fn getting every raw line).

    With status_fn (a concurrent sweep combination - see
    scenario_runs._run_sweep_concurrent), "Time = N" banners go to
    status_fn instead - overwritten in place, not appended - so several
    combinations solving at once don't flood the scrolling log the same
    way decay mode's _throttled_solver_callback already avoids. solver_log_fn
    still receives every raw line either way.
    """
    if status_fn is None:
        return solver_log_fn or log_fn

    def callback(line):
        if _TIME_LINE_RE.match(line.strip()):
            status_fn(status_key, line.strip())
        if solver_log_fn:
            solver_log_fn(line)
    return callback


def _run_phase(case_dir, case_dir_wsl, n_iterations, write_interval, window_frac, plateau_rel_tol,
                log_fn, should_stop=None, solver_log_fn=None, live_monitoring_zones=(),
                live_patches=(), check_interval=None, t_inf_rel_tol=None, t_inf_streak=3,
                keep_all_timesteps=False, iteration_offset=0, mass_balance_patches=(),
                injection_rate_G=None, mass_balance_tol=None, status_fn=None, status_key=None,
                should_pause=None, delta_t=1):
    """Run simpleFoam for n_iterations, tracking the room-wide (and any
    monitoring-point) volAverage(T) live, every iteration.

    keep_all_timesteps: if True, every write_interval snapshot directory is
    kept (renamed to its true cumulative iteration count) instead of being
    deleted between/after chunks - lets ParaView play back the whole run,
    not just the initial/final states. iteration_offset shifts the renamed
    numbers by however many iterations already ran in an earlier phase
    (e.g. phase 2 passes phase 1's final iteration count), so a caller
    running multiple phases back-to-back in the same case directory gets
    one continuous, collision-free numbering instead of both phases
    starting their directory names back at 1.

    check_interval/t_inf_rel_tol/t_inf_streak: optional early-stop via
    T-infinity extrapolation stability (see decay_analysis.
    fit_asymptotic_value/check_t_infinity_stability). When t_inf_rel_tol
    is given, the phase runs in chunks of check_interval iterations
    (each a fresh simpleFoam invocation starting from whatever 0/ holds -
    same "run a chunk, copy converged fields back to 0/, clean time dirs"
    pattern already proven in run_pipeline.converge_flow_field), re-fitting
    the extrapolated T-infinity from the accumulated live series after
    each chunk, and stopping once t_inf_streak consecutive estimates
    agree within t_inf_rel_tol - rather than always running the full
    n_iterations budget. Purely an early exit: n_iterations remains the
    hard upper bound regardless (if T-infinity never stabilizes,
    behavior is unchanged from before this feature existed).
    t_inf_rel_tol=None (the default) disables this entirely -
    check_interval then defaults to n_iterations, i.e. one chunk, the
    original single-shot behavior.

    Since fields get copied back to 0/ and time dirs cleaned after every
    chunk (to keep a long, potentially-many-chunk run's case directory
    lightweight), there's no lasting on-disk history to postProcess
    against at the end - the live per-iteration series (accumulated in
    Python across chunks, each chunk's own local time labels offset by
    the iteration count already run) is the only source of truth, for
    both the T-infinity fit and the returned decay_curve (downsampled
    from it at write_interval cadence, replacing the old separate
    `postProcess -dict system/volAverageDict` pass entirely - result_figures.py
    already prefers "live" over "decay_curve" wherever both exist, so
    this is a safe, compatible substitution).

    mass_balance_patches: if given, also live-tracks each patch's flow
    rate and flow-weighted T every iteration (see contaminant_source.
    live_mass_balance_functions) - lets the caller compute a proper
    trailing-window mass-balance ratio (contaminant_source.
    windowed_mass_balance) instead of trusting a single instantaneous
    snapshot (see check_mass_balance's docstring for why that matters -
    confirmed directly that a windowed ratio is stable/low-noise, ~0.5%
    CV, where a naive short-window derivative proxy was badly biased).
    Returned via the new mass_balance_flux entry in the accumulated dict.

    injection_rate_G/mass_balance_tol: when given (together with
    mass_balance_patches), acceptance requires mass balance to *also* be
    within tolerance, not just T-infinity stability alone - confirmed
    directly necessary on two separate real cases: a cold-start ACH=1.5
    run accepted with mass balance at 83% (fit_cv was tight, 0.18% - a
    well-constrained fit that was still wrong), and an ACH=1 run accepted
    with mass balance at just 6.3% (fit_cv was loose, 3.28% - a poorly-
    constrained fit that coincidentally satisfied the streak by chance).
    Mass balance was the one signal that would have caught BOTH failures;
    T-infinity stability alone isn't sufficient. A fit whose own fit_cv
    exceeds t_inf_rel_tol is treated as if it had failed entirely (never
    contributes to the streak) - a cheap first filter for the second
    failure mode, checked before the (pricier) mass-balance measurement.

    delta_t: OpenFOAM's own pseudo-time step, must be a positive integer
    (default 1, the historical behavior). simpleFoam's own U/p/k/omega
    solve has no ddt() term at all (pure SIMPLE relaxation, unaffected by
    deltaT), but scalarTransport1's T equation does - solved implicitly
    (unconditionally stable), so scaling deltaT up lets a fixed, cheap
    iteration budget cover more real residence time for T's own buildup,
    fully decoupled from the frozen flow field (see compute_scaled_delta_t
    and the "4-6 residence times" criterion this is built to satisfy -
    confirmed directly: a 1500/1000-iteration run with scaled deltaT
    matched a 4000/2500-iteration run at deltaT=1 almost exactly, where an
    unscaled 1500/1000 run badly undershot). n_iterations/total_run/
    chunk_size remain real ITERATION counts throughout (unchanged
    semantics, unchanged compute cost) - delta_t only stretches how much
    OpenFOAM time each of those iterations represents. Every directory
    name/OpenFOAM time value this function touches is therefore always an
    exact multiple of delta_t (end_time and write_interval are scaled by
    the same integer factor before being handed to set_control_dict_time),
    so `int()`-based directory-name parsing stays exact - no float drift
    to guard against. Only 1 (the default) is supported together with
    keep_all_timesteps=True; combining them raises, since the cumulative-
    iteration renaming in _rename_chunk_time_dirs assumes directory names
    and iteration offsets share the same units.
    """
    if delta_t != 1 and keep_all_timesteps:
        raise ValueError("_run_phase: keep_all_timesteps is not supported together with delta_t != 1 "
                          "(cumulative-iteration renaming assumes directory names are iteration counts).")
    check_interval = check_interval or n_iterations
    if not keep_all_timesteps:
        _clean_time_dirs(case_dir_wsl)

    # Live (every-iteration) volAverage tracking - splice into controlDict's
    # functions{} block, alongside whatever's already there (e.g.
    # scalarTransport1) - see monitoring.live_vol_average_functions and
    # splice.splice_into_functions_block. Room-wide tracking is always on;
    # live_monitoring_zones adds one more live tracker per monitoring point.
    # Idempotent: controlDict persists across both phases (never
    # regenerated in between), so a phase-2 call would otherwise splice a
    # second, duplicate copy of the same named entries - only splice once.
    mass_balance_block_names = [n for p in mass_balance_patches
                                 for n in (f"{p}FlowRateLive", f"{p}FlowWeightedTLive")]
    live_block_names = (["volAverageLive1"] + [f"{p}AverageLive" for p in live_patches]
                         + [f"monitor_{z}Live" for z in live_monitoring_zones] + mass_balance_block_names)
    with open(f"{case_dir}/system/controlDict") as f:
        controldict_content = f.read()
    if "volAverageLive1" not in controldict_content:
        block = live_vol_average_functions(
            field="T", patches=live_patches, monitoring_zones=live_monitoring_zones)
        _, n_open, n_close = splice_into_functions_block(case_dir, block)
        assert n_open == n_close, f"Brace mismatch after live-volAverage splice: {n_open} vs {n_close}"
    if mass_balance_patches and "FlowRateLive" not in controldict_content:
        mb_block = live_mass_balance_functions(mass_balance_patches)
        _, n_open, n_close = splice_into_functions_block(case_dir, mb_block)
        assert n_open == n_close, f"Brace mismatch after live-mass-balance splice: {n_open} vs {n_close}"

    accumulated = {"room": ([], [])}
    for zone in live_monitoring_zones:
        accumulated[zone] = ([], [])
    mb_flow = {p: [] for p in mass_balance_patches}
    mb_weighted_t = {p: [] for p in mass_balance_patches}
    tinf_history = []
    stopped_via_tinf = False
    total_run = 0
    final_dir_name = None

    while total_run < n_iterations:
        chunk_size = min(check_interval, n_iterations - total_run)
        # end_time/write_interval are OpenFOAM TIME values, not iteration
        # counts - scale both by delta_t (an integer, see docstring) so
        # every value handed to OpenFOAM stays an exact integer, and every
        # directory this chunk writes lands at an exact multiple of
        # delta_t.
        end_time = chunk_size * delta_t
        chunk_write_interval = _chunk_write_interval(write_interval * delta_t, end_time)
        set_control_dict_time(case_dir, end_time=end_time,
                               write_interval=chunk_write_interval, delta_t=delta_t)
        # set_control_dict_time's sweep above touches every writeInterval
        # in the file, including these live blocks (left over from an
        # earlier chunk/phase) - re-pin them to 1 without touching the
        # main solve's own writeInterval.
        for name in live_block_names:
            set_function_write_interval(case_dir, name, 1)

        # Snapshot before this chunk's own solve, so the directories it
        # creates can be identified exactly (by diff) rather than guessed
        # at from a numeric range - see _rename_chunk_time_dirs' docstring
        # for the corruption this fixes.
        dirs_before = _list_time_dirs(case_dir_wsl)

        log_fn(f"Running simpleFoam ({total_run + 1}-{total_run + chunk_size} of {n_iterations} "
               f"iterations, writing every {write_interval})...")
        r = run_wsl_streaming(
            "simpleFoam 2>&1 | tee log.simpleFoam", case_dir_wsl,
            on_line=_phase_solver_callback(log_fn, solver_log_fn, status_fn, status_key),
            should_stop=should_stop, kill_pattern="simpleFoam", should_pause=should_pause,
        )
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped during simpleFoam phase.")
        if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
            tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
            raise RuntimeError(f"simpleFoam failed (exit {r.returncode}):\n{tail}")

        dirs_after = _list_time_dirs(case_dir_wsl)
        new_dirs = dirs_after - dirs_before
        if not new_dirs:
            raise RuntimeError("simpleFoam did not write any new time directory")
        latest = max(new_dirs, key=int)

        # This chunk's own live tracking - every chunk starts fresh at
        # time-label "0" (startFrom/startTime are never changed in this
        # pipeline), so its own postProcessing output only covers this
        # chunk's iterations. The live blocks are re-pinned to writeInterval=1
        # above, in OpenFOAM TIME units - with delta_t != 1, their own "t"
        # column is therefore real OpenFOAM time (0, delta_t, 2*delta_t, ...),
        # not a raw iteration count. Divide by delta_t first (exact - every
        # live-tracked step lands at a whole multiple of delta_t) to keep
        # acc_t/the returned live/decay_curve series in ITERATION units
        # throughout, unaffected by delta_t (same meaning fit_asymptotic_value/
        # windowed_stats/result_figures.py already assume), then offset by
        # total_run before appending, to build one continuous global series
        # across chunks.
        chunk_t, chunk_T = read_vol_average_dat(f"{case_dir}/postProcessing/volAverageLive1/0/volFieldValue.dat")
        chunk_t = chunk_t / delta_t
        acc_t, acc_T = accumulated["room"]
        acc_t.extend((chunk_t + total_run).tolist())
        acc_T.extend(chunk_T.tolist())
        for zone in live_monitoring_zones:
            zt, zT = read_vol_average_dat(f"{case_dir}/postProcessing/monitor_{zone}Live/0/volFieldValue.dat")
            zt = zt / delta_t
            azt, azT = accumulated[zone]
            azt.extend((zt + total_run).tolist())
            azT.extend(zT.tolist())
        for patch in mass_balance_patches:
            _, pflow = read_vol_average_dat(f"{case_dir}/postProcessing/{patch}FlowRateLive/0/surfaceFieldValue.dat")
            _, pT = read_vol_average_dat(f"{case_dir}/postProcessing/{patch}FlowWeightedTLive/0/surfaceFieldValue.dat")
            mb_flow[patch].extend(pflow.tolist())
            mb_weighted_t[patch].extend(pT.tolist())

        # NOT chunk_size (the requested budget) - simpleFoam's own SIMPLE
        # residualControl (on U/p/(k|omega), inherited from the same
        # fvSolution flow convergence uses) can trigger "SIMPLE solution
        # converged" and exit long before reaching the requested endTime,
        # completely independent of T's own state (T is a passive scalar,
        # not part of that residual check) - confirmed directly on a real
        # ACH=1 run where every single 500-iteration chunk actually only
        # ran ~16-19 iterations this way, so total_run was silently
        # overcounting by ~30x (15000 "counted" vs ~500 real iterations of
        # actual T build-up). `latest` is the chunk's own true iteration
        # count regardless of why it stopped (a normal full chunk writes
        # its final snapshot named after chunk_size exactly, so this is a
        # no-op change for the common case) - trust it, not the request.
        # latest is an OpenFOAM TIME value (see docstring) - always an exact
        # multiple of delta_t (SIMPLE's own residualControl, if it ever
        # triggers an early stop, still only ever writes at a whole pseudo-
        # time step), so integer division recovers the true iteration count.
        chunk_start = total_run
        total_run += int(latest) // delta_t

        # Attempted on every chunk (including the final one, not just while
        # there's still budget left) - this is the NEW primary readiness
        # signal for Phase 1 (see Phase1ExtrapolationUndecided), not just an
        # early-stop nicety, so the caller needs to know whether acceptance
        # happened at all by the time the loop ends, even if that happened
        # to land exactly on the last chunk rather than "early".
        stop_early = False
        if t_inf_rel_tol is not None:
            fit = fit_asymptotic_value(np.array(acc_t), np.array(acc_T))
            # A numerically-successful fit that isn't well-constrained is
            # treated as no fit at all - otherwise a run of noisy estimates
            # can coincidentally land within t_inf_rel_tol of each other
            # and satisfy the streak by chance (confirmed directly: a real
            # ACH=1 run's accepted fit had fit_cv=3.28%, and its mass
            # balance came out at 6.3% - nowhere near converged).
            if fit is not None and fit["fit_cv"] is not None and fit["fit_cv"] > t_inf_rel_tol:
                fit = None
            tinf_history.append(fit["Tinf"] if fit else None)
            extrapolation_stable = check_t_infinity_stability(tinf_history, rel_tol=t_inf_rel_tol, streak=t_inf_streak)

            mass_balance_ok = True
            mb_ratio_text = ""
            if extrapolation_stable and mass_balance_patches and injection_rate_G and mass_balance_tol is not None:
                mb_check = windowed_mass_balance(np.array(acc_t), mb_flow, mb_weighted_t, injection_rate_G,
                                                  window_frac=window_frac, tol=mass_balance_tol)
                mass_balance_ok = mb_check["within_tolerance"]
                mb_ratio_text = f", mass balance {mb_check['ratio']:.1%}"
                if not mass_balance_ok:
                    log_fn(f"  T-infinity stable, but mass balance ({mb_check['ratio']:.1%}) isn't yet - "
                           f"not accepting Phase 1 as done.")

            if extrapolation_stable and mass_balance_ok:
                stopped_via_tinf = True
                if total_run < n_iterations:
                    log_fn(f"  T-infinity stable ({t_inf_streak}x within {t_inf_rel_tol:.0%}{mb_ratio_text}) - "
                           f"stopping early at {total_run}/{n_iterations} iterations.")
                    stop_early = True

        if stop_early or total_run >= n_iterations:
            # Final chunk - leave its own directory in place (renamed to
            # its true CUMULATIVE iteration count; each chunk's own
            # OpenFOAM-assigned directory name restarts from 1, see
            # docstring) rather than copying-back-and-cleaning like every
            # earlier chunk needed for continuation. The caller decides
            # whether to keep or clean this final directory (mirrors the
            # pre-chunking behavior exactly: phase 1's final directory
            # gets cleaned by the caller since only phase 2's matters for
            # standalone ParaView viewing; phase 2's is deliberately kept).
            #
            # This chunk may have written its OWN intermediate snapshots
            # too (at write_interval, e.g. a 500-iteration chunk with
            # write_interval=100 writes 100/200/300/400/500) - only the
            # last (`latest`) is the true final state; the others carry
            # chunk-LOCAL labels that don't match the real cumulative
            # iteration count (misleading if left around - "100" could
            # really be iteration 1600) and must be removed, not kept -
            # unless keep_all_timesteps opted into preserving them (renamed
            # to their true cumulative count) instead.
            if keep_all_timesteps:
                offset = iteration_offset + chunk_start
                _rename_chunk_time_dirs(case_dir_wsl, offset, new_dirs)
                # The true final directory name after renaming - NOT
                # necessarily total_run + iteration_offset (that assumes a
                # snapshot landed exactly at chunk_size, which adjustableRunTime
                # writeControl doesn't guarantee - confirmed as a real bug:
                # this mismatch is what caused a later step to fail looking
                # for a directory that was never actually written).
                final_dir_name = str(int(latest) + offset)
            else:
                run_wsl_or_raise(
                    f'for d in [0-9]*/; do [ "$d" = "0/" ] || [ "$d" = "{latest}/" ] || rm -rf "$d"; done',
                    case_dir_wsl, "clearing this chunk's other intermediate snapshots",
                )
                if latest != str(total_run):
                    run_wsl_or_raise(f"mv {latest} {total_run}", case_dir_wsl,
                                      "renaming final chunk's time directory")
                final_dir_name = str(total_run)
            run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing this chunk's postProcessing")
            break

        log_fn(f"  Copying fields from {latest}/ to 0/ so the next chunk continues from here...")
        _copy_latest_to_zero(case_dir_wsl, latest, include_T=True, log_fn=log_fn)
        run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing this chunk's postProcessing")
        if keep_all_timesteps:
            _rename_chunk_time_dirs(case_dir_wsl, iteration_offset + chunk_start, new_dirs)
        else:
            _clean_time_dirs(case_dir_wsl)

    live_curves = {zone: (np.array(vals[0]), np.array(vals[1])) for zone, vals in accumulated.items()}
    live_t, live_T = live_curves["room"]

    # Sparse ("decay_curve") series for result_figures.py's fallback/older-
    # results-file path - downsampled from the dense live series rather
    # than a separate OpenFOAM postProcess call (see docstring).
    stride = max(1, int(write_interval))
    sparse_t, sparse_T = live_t[::stride], live_T[::stride]
    if len(live_t) and (len(sparse_t) == 0 or sparse_t[-1] != live_t[-1]):
        sparse_t = np.append(sparse_t, live_t[-1])
        sparse_T = np.append(sparse_T, live_T[-1])

    converged, cv = check_plateau_windowed(live_t, live_T, frac=window_frac, rel_tol=plateau_rel_tol)
    cv_text = f"{cv * 100:.2f}%" if cv is not None else "n/a"
    log_fn(f"  Stopped at time {total_run}. T_ss={live_T[-1]:.4g} (trailing-{window_frac:.0%} CV={cv_text}, "
           f"{'plateaued' if converged else 'NOT YET PLATEAUED - consider more iterations'})")
    # The on-disk final directory's actual name (set inside the loop's
    # final-chunk branch, from what renaming truly produced - NOT assumed
    # from total_run + iteration_offset, which isn't guaranteed to be a
    # directory that was ever actually written, see the loop body) -
    # callers doing further I/O against the final directory (e.g.
    # _copy_latest_to_zero) need this, while anything just reporting "how
    # many iterations did this phase run" wants the unshifted total_run
    # instead (returned separately).
    assert final_dir_name is not None, "loop must run at least one chunk (n_iterations > 0)"
    mass_balance_flux = {"flow": mb_flow, "weighted_t": mb_weighted_t}
    return (final_dir_name, total_run, sparse_t, sparse_T, converged, live_curves,
            stopped_via_tinf, tinf_history, mass_balance_flux)


def _room_phase_summary(live_room, window_frac, converged, iterations, sparse_t, sparse_T, log_fn):
    """Room-wide phase1/phase2 entry: T_ss is the trailing-window mean of
    the live per-iteration series (not the single last sample) - see
    windowed_stats. T_ss_std/T_ss_cv are the DETRENDED version
    (windowed_stats_detrended) - a raw window std/CV conflates genuine
    fluctuation with a still-slowly-changing average, which isn't what a
    user checking "is this noisy" wants (see CHANGELOG); plateau/
    convergence detection is unaffected, still on the raw statistic (see
    check_plateau_windowed). Also attempts an exponential-approach
    extrapolation to the true n->infinity value (fit_asymptotic_value) -
    a windowed average is provably biased whenever the curve hasn't fully
    flattened within the run's iteration budget, confirmed on a real run
    (windowed averages at multiple window widths were all ~3% off a
    well-fit extrapolation). None when the fit doesn't converge/isn't
    available - not an error, just "couldn't extrapolate this one."
    `decay_curve`/`live` (sparse postProcess read / dense per-iteration
    read) are both kept as-is for result_figures.py.
    """
    live_t, live_T = live_room
    mean, _, _, n, span = windowed_stats(live_t, live_T, frac=window_frac)
    _, std, cv, _, _ = windowed_stats_detrended(live_t, live_T, frac=window_frac)
    cv_text = f"{cv * 100:.1f}%" if cv is not None else "n/a"
    log_fn(f"  Moving average (last {span:.4g} iterations, n={n}): {mean:.4g} (residual CV={cv_text})")
    extrap = fit_asymptotic_value(live_t, live_T)
    if extrap is not None:
        log_fn(f"  Extrapolated T-infinity (exponential-approach fit): {extrap['Tinf']:.4g} "
               f"(tau={extrap['tau']:.4g} iterations, fit CV={extrap['fit_cv'] * 100:.2f}%)")
    return {
        "T_ss": mean, "T_ss_std": std, "T_ss_cv": cv, "T_ss_window_span": span,
        "T_ss_window_n": n, "T_ss_window_frac": window_frac,
        "T_inf_extrapolated": extrap["Tinf"] if extrap else None,
        "T_inf_extrapolation_detail": extrap,
        "converged": converged, "iterations": iterations,
        "decay_curve": {"t": sparse_t.tolist(), "T": sparse_T.tolist()},
        "live": {"t": live_t.tolist(), "T": live_T.tolist()},
    }


def _point_phase_summary(live_point, window_frac):
    """Same windowed treatment as _room_phase_summary, for one monitoring
    point's phase1/phase2 entry. Keeps the t_seconds/volAverage_T key names
    report.py/monitoring_points.mixing_uniformity_note already expect
    (misnomer for steady-state's pseudo-iteration t, kept for continuity).
    """
    t, T = live_point
    mean, _, _, n, span = windowed_stats(t, T, frac=window_frac)
    _, std, cv, _, _ = windowed_stats_detrended(t, T, frac=window_frac)
    return {
        "T_ss": mean, "T_ss_std": std, "T_ss_cv": cv, "T_ss_window_span": span,
        "T_ss_window_n": n, "T_ss_window_frac": window_frac,
        "t_seconds": t.tolist(), "volAverage_T": T.tolist(),
    }


def _phase1_checkpoint_path(case_dir):
    return f"{case_dir}/phase1_checkpoint.json"


def _write_phase1_checkpoint(case_dir, phase1_summary, phase1_monitoring, G, Su, source_volume,
                              n_source_cells):
    """Persist everything Phase 2 (and the final results summary) needs
    from a just-completed Phase 1, so a run that stops anywhere after this
    point - a crash, a bug in the bookkeeping that follows, or just
    stopping the app - can resume straight into Phase 2 next time instead
    of redoing Phase 1's own (often the more expensive) iteration budget.

    This is the general form of a real recovery done by hand once already:
    a run's Phase 1 had genuinely converged and plateaued, but a directory-
    naming bug crashed the very next bookkeeping step, and reconstructing
    G/Su/T_ss from logs and re-deriving the converged field from on-disk
    timestamps was only possible because those values happened to still be
    inferable - this checkpoint means that reconstruction is never needed
    again; the real values are just read back.
    """
    data = {
        "phase1_summary": phase1_summary, "phase1_monitoring": phase1_monitoring,
        "G": G, "Su": Su, "source_volume": source_volume, "n_source_cells": n_source_cells,
    }
    with open(_phase1_checkpoint_path(case_dir), "w") as f:
        json.dump(data, f, indent=2)


def _read_phase1_checkpoint(case_dir):
    path = _phase1_checkpoint_path(case_dir)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _clear_phase1_checkpoint(case_dir):
    Path(_phase1_checkpoint_path(case_dir)).unlink(missing_ok=True)


def _phase1_pending_path(case_dir):
    return f"{case_dir}/phase1_pending.json"


def _write_phase1_pending(case_dir, G, Su, source_volume, n_source_cells):
    """Persisted once, right before Phase 1's own solve starts (before
    _phase1_checkpoint.json exists) - lets a fresh process resume a
    Phase1ExtrapolationUndecided decision (read G/Su back rather than
    recompute) without needing the source cellZone re-carved or T reset,
    since 0/ already holds Phase 1's current (mid-run, undecided) state.
    Cleared once Phase 1 either finishes (checkpoint written) or the
    whole scenario is abandoned.
    """
    data = {"G": G, "Su": Su, "source_volume": source_volume, "n_source_cells": n_source_cells}
    with open(_phase1_pending_path(case_dir), "w") as f:
        json.dump(data, f, indent=2)


def _read_phase1_pending(case_dir):
    path = _phase1_pending_path(case_dir)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _clear_phase1_pending(case_dir):
    Path(_phase1_pending_path(case_dir)).unlink(missing_ok=True)


def run_steady_state_scenario(case_dir, room_x, room_y, room_z, ach, Z, nbins=25,
                               source_center=None, source_size=0.3, target_T_ss=0.3,
                               cell_size=0.1, inlet_velocity=(0.278, 0, 0),
                               inlet2_velocity=None, has_outlet2=False,
                               inlet_diffuser_type="direct", inlet_wall=None,
                               inlet_center=None, inlet_size=None,
                               inlet2_diffuser_type="direct", inlet2_wall=None,
                               inlet2_center=None, inlet2_size=None,
                               phase1_iterations=8000, phase1_write_interval=200,
                               phase2_iterations=3000, phase2_write_interval=100,
                               plateau_rel_tol=0.01, window_frac=0.15,
                               t_inf_check_interval=None, t_inf_rel_tol=None, t_inf_streak=3,
                               keep_all_timesteps=False, mass_balance_tol=0.10,
                               phase1_t_initial=0.0, phase1_extrapolation_gate=True,
                               phase1_resume_decision=None, phase1_resume_additional_iterations=None,
                               fan_entry=None, monitoring_points=None,
                               patches_to_monitor=("outlet",), log_fn=print, should_stop=None,
                               solver_log_fn=None, status_fn=None, phase1_only=False, should_pause=None,
                               measured_ventilation_ach=None, phase1_delta_t=1, phase2_delta_t=1):
    """Run both phases of a continuous-source steady-state scenario against
    an already-converged case (mesh + flow + fluenceRate/kUV must already
    exist - see run_pipeline.setup_case()). Returns a summary dict.

    mass_balance_tol: fractional tolerance for Phase 1's mass-balance cross-
    check (contaminant_source.windowed_mass_balance) - compares the actual
    outlet removal rate (trailing-window average of the live per-iteration
    flux, NOT a single instantaneous snapshot - confirmed directly that a
    snapshot is the wrong quantity to trust, while a properly windowed
    average is stable/low-noise, ~0.5% CV) against the known injection
    rate G, a curve-fitting-free signal: at true steady state they must
    match exactly. No longer Phase 1's primary readiness gate (see
    phase1_extrapolation_gate) - demoted to an informational cross-check
    surfaced in the results, since reaching it reliably needs meaningfully
    more iterations than the cheaper T-infinity extrapolation already
    trusts (confirmed on a real run: extrapolation was accurate at ~2x the
    original budget, mass balance didn't reach 95% until ~2.8x). Phase-1-
    only: Phase 2 also removes T via the UV sink cellZones, not just
    advective outflow, so the same simple injection=removal identity
    doesn't hold there.

    phase1_t_initial: Phase 1's starting T value (0.0 by default). Used to
    warm-start at target_T_ss - confirmed directly that this is NOT
    actually faster under the extrapolation-based readiness gate: a cold
    (T=0) start reached a stable, accurate extrapolation in ~40% fewer
    iterations than a warm start on the same case, because a uniform
    target_T_ss guess doesn't match the true (often highly non-uniform)
    steady spatial pattern, forcing an extra redistribution transient on
    top of the real exponential relaxation - a cold start's curve is
    simpler (closer to the single-exponential shape fit_asymptotic_value
    assumes) and gets trusted sooner despite "starting further away."

    phase1_extrapolation_gate: if True (default), Phase 1 is not accepted
    until fit_asymptotic_value has produced a stable, accepted
    extrapolation (t_inf_streak consecutive fits agreeing within
    t_inf_rel_tol - see _run_phase) - if phase1_iterations is exhausted
    first, raises Phase1ExtrapolationUndecided rather than silently
    accepting whatever the CV-plateau check says (confirmed directly: a
    "plateaued" CV=0.56% verdict at 18000 iterations sat on a curve still
    genuinely rising, needing ~25000 iterations for mass balance to catch
    up - the CV-plateau check alone isn't a reliable Phase-1-done signal).
    If False, falls back to the old behavior (CV-plateau/hard-budget only,
    T-infinity early-stop purely a speed optimization if enabled) - an
    escape hatch, not the recommended setting.

    phase1_resume_decision/phase1_resume_additional_iterations: set by a
    caller resuming a Phase1ExtrapolationUndecided decision (see that
    exception and _read_phase1_pending) - "continue" runs
    phase1_resume_additional_iterations more iterations from Phase 1's
    current (already mid-run) state without re-carving the source zone or
    resetting T; "accept" treats the current state as final regardless of
    whether extrapolation ever stabilized (flagged as such in the result).
    Only meaningful when phase1_pending.json exists (Phase 1 was already
    attempted and left undecided) - ignored otherwise.

    t_inf_check_interval/t_inf_rel_tol/t_inf_streak: chunk size / stability
    tolerance / consecutive-agreement streak for T-infinity extrapolation
    stability (see _run_phase/decay_analysis.check_t_infinity_stability) -
    t_inf_rel_tol is None by default (disabled; each phase always runs its
    full phaseN_iterations budget). GUI-exposed as a cross-project
    "advanced" setting (Settings menu, right of File). For Phase 1, this is
    now the primary readiness signal (see phase1_extrapolation_gate), not
    just an early-stop nicety - Phase 2 still treats it as early-stop-only.

    keep_all_timesteps: if True, every write_interval snapshot from both
    phases is kept on disk (renamed to one continuous, collision-free
    cumulative iteration count spanning phase 1 then phase 2) instead of
    being deleted down to just the initial/final state - lets ParaView
    play back the whole run. Off by default: a long/fine-grained run can
    leave a lot of snapshot directories behind, so this is opt-in. Not
    supported together with phase1_delta_t/phase2_delta_t != 1 (see
    _run_phase) - raises if both are requested at once.

    phase1_delta_t/phase2_delta_t: OpenFOAM pseudo-time step for each
    phase (default 1 each, the historical behavior) - see _run_phase's
    own delta_t docstring and compute_scaled_delta_t. Lets a phase's
    fixed phaseN_iterations budget cover more real residence time for
    low-ACH cases, without costing any more compute - the caller is
    expected to compute these via compute_scaled_delta_t (using this
    phase's effective removal rate: ach for Phase 1, ach+eACH_uv for
    Phase 2) rather than pass an arbitrary value.

    fan_entry: pre-built fvOptions entry text (see fan.fan_fvoptions_entry())
    if a mixing fan should stay active through both phases, same "always
    on" treatment as the contaminant source itself. If the fan's cellZone
    was already carved as part of setup_case()'s flow convergence (so the
    converged flow field already reflects the fan's influence), just pass
    the same entry text again here - no need to re-carve the zone.

    monitoring_points: optional list of monitoring_points.py-shaped point
    dicts. Each point's cellZone is carved once, up front (topoSet is
    mesh-only, and the mesh is fixed for both phases), then tracked live
    every solver iteration alongside the room average (see
    monitoring.live_vol_average_functions) - both room-wide T and every
    monitoring point report a windowed mean/std/CV (decay_analysis.
    windowed_stats over the trailing `window_frac` fraction of the live
    per-iteration series) instead of a single noisy last-sample read,
    which real turbulent rooms can be off by 25-50%+ on for small
    monitoring volumes (see the live-volAverage validation).

    window_frac: fraction of each phase's live per-iteration samples used
    for the trailing-window mean/std/CV (T_ss and every monitoring point).
    Persisted per-phase as T_ss_window_frac so historical reports stay
    correct even if this default changes for future runs.

    status_fn(key, line_or_None), if given (a concurrent sweep combination
    - see scenario_runs._run_sweep_concurrent), receives each phase's
    latest "Time = N" line to display in place instead of the scrolling
    log - see _phase_solver_callback.

    should_pause: forwarded straight through to each _run_phase call's
    run_wsl_streaming - suspends the active simpleFoam process in place
    (no iterations lost, no exception raised) instead of killing it,
    unlike should_stop.

    measured_ventilation_ach: a ventilation-only rate [1/hr] measured by a
    separate UV-off control run (see scenario_runs._run_shared_control),
    if one was run. When given, eACH_uv_steady_state_corrected uses
    compute_corrected_eACH_uv_from_control (Phase 2's T_ss2 minus this
    measured rate) instead of deriving the ventilation rate from Phase 1's
    own T_ss1 (compute_corrected_eACH_uv) - see the former's docstring for
    why that matters whenever the source zone is small/localized.

    phase1_only: stop right after Phase 1 finishes (and its checkpoint is
    written) instead of continuing into Phase 2 - used by
    scenario_runs._run_shared_phase1 to run Phase 1 ONCE per ACH group in
    a shared directory (Phase 1's own physics - injection strength G/Su -
    depends only on ach/target_T_ss/source geometry, none of which vary
    with Z, the same way decay mode's UV-off control doesn't depend on Z).
    Every Z sharing that ACH then clones the shared, phase1-converged
    directory and calls this function normally (phase1_only=False) - the
    existing checkpoint-detection logic below picks up the already-
    converged state and skips straight to Phase 2, without needing to
    know phase1_only was ever used.
    """
    case_dir_wsl = wsl_path(case_dir)
    room_volume = room_x * room_y * room_z
    if source_center is None:
        source_center = (room_x / 2, room_y / 2, 1.6)
    summary = {"room_volume": room_volume, "source_center": source_center, "target_T_ss": target_T_ss}

    run_wsl_or_raise("touch case.foam", case_dir_wsl, "touching case.foam")

    log_fn("Ensuring SIMPLE fvSolution and outlet-average monitoring are set up...")
    ensure_simple_fvsolution(case_dir)
    # Flow convergence (setup_case()) is already done by this point, so the
    # residualControl block that was useful there would only hurt now - it
    # would let simpleFoam declare "converged" and exit a Phase 1/2 chunk
    # after ~16-19 iterations based on the (already-converged) flow field,
    # starving T of the iterations each chunk is actually meant to deliver.
    disable_simple_residual_control(case_dir)
    write_vol_average_dict(case_dir, field="T", patches=patches_to_monitor)

    # Carve monitoring cellZones now, before either phase's solve, instead
    # of a post-hoc pass after each phase - topoSet is mesh-only (not
    # field-dependent), and the mesh is fixed for the rest of this
    # scenario, so the zones carved here stay valid for both phase 1 and
    # phase 2's live function objects. Always redone (cheap, idempotent),
    # even when resuming from a Phase 1 checkpoint below.
    live_zone_names = []
    if monitoring_points:
        write_monitoring_topo_set_dict(case_dir, monitoring_points, cell_size)
        run_wsl_or_raise("topoSet -dict system/monitoringTopoSetDict", case_dir_wsl,
                          "topoSet (monitoring zones)")
        live_zone_names = [zone_name(p["name"]) for p in monitoring_points]

    checkpoint = _read_phase1_checkpoint(case_dir)
    pending = _read_phase1_pending(case_dir) if checkpoint is None else None
    iters1 = 0
    if checkpoint is not None:
        # Phase 1 already ran and converged in an earlier attempt (see
        # _write_phase1_checkpoint) - reuse its result instead of redoing
        # potentially the more expensive of the two phases. 0/ already
        # holds Phase 1's converged T field (copied there right after it
        # finished, same as always), so this genuinely just continues -
        # no source re-carving, no T reset, no re-running _run_phase.
        log_fn("Found a Phase 1 checkpoint from an earlier attempt - skipping Phase 1 entirely and "
               "resuming straight into Phase 2 with its already-converged state.")
        G, Su = checkpoint["G"], checkpoint["Su"]
        source_volume = checkpoint["source_volume"]
        summary["source_Su"] = Su
        summary["source_volume"] = source_volume
        summary["injection_rate_total"] = G
        summary["phase1"] = checkpoint["phase1_summary"]
        phase1_monitoring = checkpoint["phase1_monitoring"]
        iters1 = checkpoint["phase1_summary"]["iterations"]
        source_entry = source_fvoptions_entry(Su)
        fan_entries = [fan_entry] if fan_entry is not None else []
    else:
        if pending is not None and phase1_resume_decision is not None:
            # Resuming a Phase1ExtrapolationUndecided decision - 0/ already
            # holds Phase 1's current (mid-run, undecided) state, and the
            # source cellZone/fvOptions/BCs are already set up from the
            # earlier attempt - do NOT re-carve or reset T (see
            # phase1_resume_decision's docstring).
            log_fn(f"Resuming Phase 1's undecided state (decision: {phase1_resume_decision})...")
            G, Su = pending["G"], pending["Su"]
            source_volume, n_source_cells = pending["source_volume"], pending["n_source_cells"]
            summary["source_Su"] = Su
            summary["source_volume"] = source_volume
            summary["injection_rate_total"] = G
            source_entry = source_fvoptions_entry(Su)
            fan_entries = [fan_entry] if fan_entry is not None else []

            if phase1_resume_decision == "continue":
                additional = phase1_resume_additional_iterations or phase1_iterations
                log_fn(f"=== Phase 1 (resumed): {additional} more iterations ===")
                run_iterations, run_check_interval, run_t_inf_rel_tol = additional, t_inf_check_interval, t_inf_rel_tol
            else:  # "accept" - sample one more window to build a live series to report against,
                   # not to re-litigate stability (the previous attempt's own per-iteration
                   # series lived only in that process's memory, not persisted - only the
                   # scalar tinf_history/G/Su in phase1_pending.json survive a fresh process).
                run_iterations = t_inf_check_interval or 500
                log_fn(f"=== Phase 1 (accepting current state): sampling {run_iterations} more "
                       f"iterations to build a representative window ===")
                run_check_interval, run_t_inf_rel_tol = None, None

            status_key1 = f"Z={Z}/ACH={ach}/Phase1"
            try:
                latest1, iters1, t1, T1, converged1, live1, stopped_via_tinf1, tinf_history1, mb_flux1 = _run_phase(
                    case_dir, case_dir_wsl, run_iterations, phase1_write_interval,
                    window_frac, plateau_rel_tol, log_fn, should_stop=should_stop,
                    solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
                    live_patches=patches_to_monitor,
                    check_interval=run_check_interval, t_inf_rel_tol=run_t_inf_rel_tol, t_inf_streak=t_inf_streak,
                    keep_all_timesteps=keep_all_timesteps, mass_balance_patches=patches_to_monitor,
                    injection_rate_G=G, mass_balance_tol=mass_balance_tol,
                    status_fn=status_fn, status_key=status_key1, should_pause=should_pause,
                    delta_t=phase1_delta_t,
                )
            finally:
                if status_fn is not None:
                    status_fn(status_key1, None)
            # Copy this attempt's final state into 0/ BEFORE deciding whether
            # to raise Phase1ExtrapolationUndecided again - _write_phase1_pending's
            # own docstring promises "0/ already holds Phase 1's current
            # (mid-run, undecided) state" for a later resume to build on, but
            # that was only ever true when this attempt got ACCEPTED - if it
            # raised again, the copy below never ran, so 0/ stayed however
            # many chunks stale it was from the SAME attempt (every non-final
            # chunk inside _run_phase's own loop copies its own latest to 0/,
            # but the final chunk before a raise does not). A further resume
            # would then silently continue from that stale point while
            # reporting iteration counts as if it hadn't - confirmed as a
            # real gap on a live run, not just theoretical.
            _copy_latest_to_zero(case_dir_wsl, latest1, include_T=True, log_fn=log_fn)
            if (phase1_resume_decision == "continue" and phase1_extrapolation_gate
                    and t_inf_rel_tol is not None and not stopped_via_tinf1):
                diagnostic = _phase1_extrapolation_diagnostic(
                    tinf_history1, t_inf_streak, t_inf_rel_tol, run_iterations,
                    t_inf_check_interval or run_iterations)
                raise Phase1ExtrapolationUndecided(
                    f"Phase 1 extrapolation still undecided after {additional} more iterations "
                    f"(resumed) - {diagnostic['summary']}", diagnostic, iters1)

            summary["phase1"] = _room_phase_summary(live1["room"], window_frac, converged1, iters1, t1, T1, log_fn)
            summary["phase1"]["mass_balance"] = windowed_mass_balance(
                live1["room"][0], mb_flux1["flow"], mb_flux1["weighted_t"], G, window_frac, mass_balance_tol)
            if phase1_resume_decision == "accept":
                summary["phase1"]["accepted_by_user_before_stable_extrapolation"] = True
            phase1_monitoring = {
                p["name"]: _point_phase_summary(live1[zone_name(p["name"])], window_frac)
                for p in (monitoring_points or [])
            }
            run_wsl_or_raise("cp 0/T phase1_T.snapshot", case_dir_wsl,
                              "saving Phase 1's final T field for later spatial-mixing analysis")
            if not keep_all_timesteps:
                _clean_time_dirs(case_dir_wsl)
            _clear_phase1_pending(case_dir)
            _write_phase1_checkpoint(case_dir, summary["phase1"], phase1_monitoring, G, Su, source_volume,
                                      n_source_cells)
        else:
            log_fn(f"Carving source cellZone at {source_center}, size {source_size}...")
            write_source_topo_set_dict(case_dir, source_center, source_size, cell_size=cell_size)
            r = run_wsl_or_raise("topoSet -dict system/sourceTopoSetDict", case_dir_wsl, "topoSet (source zone)")
            m = re.search(r"cellSet sourceZoneCells now size (\d+)", r.stdout)
            if not m:
                raise RuntimeError(f"Could not parse source cell count from topoSet output:\n{r.stdout}")
            n_source_cells = int(m.group(1))
            source_volume = n_source_cells * cell_size ** 3
            log_fn(f"  {n_source_cells} cells, source_volume={source_volume:.4g} m^3")

            G = compute_source_strength(room_volume, ach, target_T_ss)
            Su = source_Su(G, source_volume)
            summary["source_Su"] = Su
            summary["source_volume"] = source_volume
            # G is the total room-wide generation rate: T[amount]/m^3 * m^3/s = T[amount]/s
            # (e.g. CFU/s if T represents CFU/m^3 - see the T-field note in the report).
            summary["injection_rate_total"] = G
            log_fn(f"  G={G:.4g}, Su={Su:.4g}")

            source_entry = source_fvoptions_entry(Su)
            fan_entries = [fan_entry] if fan_entry is not None else []

            # setup_case() already resolved "ceiling"-diffuser inlets into a
            # per-face velocity list once; mapFields/flow-convergence's own
            # restore_boundary_conditions() calls (inside setup_case()) may have
            # since overwritten 0/U with that resolved value, but this scenario
            # starts by explicitly rewriting boundary conditions again - re-resolve
            # the same way here rather than assuming the plain inlet_velocity tuple
            # this function received is still the right BC value for a "ceiling"
            # inlet.
            if inlet_diffuser_type == "ceiling":
                v_mag = float(np.linalg.norm(inlet_velocity))
                center = opening_center(inlet_wall, room_x, room_y, room_z, inlet_center, inlet_size,
                                         cell_size=cell_size)
                extents = opening_half_extents(inlet_wall, room_x, room_y, room_z, inlet_center, inlet_size,
                                                cell_size=cell_size)
                inlet_velocity = resolve_inlet_velocity(case_dir, "inlet", inlet_wall, center, v_mag, "ceiling",
                                                         half_extents=extents)
            if inlet2_diffuser_type == "ceiling" and inlet2_velocity is not None:
                v_mag2 = float(np.linalg.norm(inlet2_velocity))
                center2 = opening_center(inlet2_wall, room_x, room_y, room_z, inlet2_center, inlet2_size,
                                          cell_size=cell_size)
                extents2 = opening_half_extents(inlet2_wall, room_x, room_y, room_z, inlet2_center, inlet2_size,
                                                 cell_size=cell_size)
                inlet2_velocity = resolve_inlet_velocity(case_dir, "inlet2", inlet2_wall, center2, v_mag2, "ceiling",
                                                          half_extents=extents2)

            # --- Phase 1: source only, no UV ---
            log_fn("=== Phase 1: source only (no UV) ===")
            write_fvoptions_file(case_dir, [source_entry] + fan_entries)
            _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
            assert n_open == n_close, f"Brace mismatch: {n_open} vs {n_close}"
            restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, T_initial=phase1_t_initial,
                                         inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2)
            _write_phase1_pending(case_dir, G, Su, source_volume, n_source_cells)

            status_key1 = f"Z={Z}/ACH={ach}/Phase1"
            try:
                latest1, iters1, t1, T1, converged1, live1, stopped_via_tinf1, tinf_history1, mb_flux1 = _run_phase(
                    case_dir, case_dir_wsl, phase1_iterations, phase1_write_interval,
                    window_frac, plateau_rel_tol, log_fn, should_stop=should_stop,
                    solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
                    live_patches=patches_to_monitor,
                    check_interval=t_inf_check_interval, t_inf_rel_tol=t_inf_rel_tol, t_inf_streak=t_inf_streak,
                    keep_all_timesteps=keep_all_timesteps, mass_balance_patches=patches_to_monitor,
                    injection_rate_G=G, mass_balance_tol=mass_balance_tol,
                    status_fn=status_fn, status_key=status_key1, should_pause=should_pause,
                    delta_t=phase1_delta_t,
                )
            finally:
                if status_fn is not None:
                    status_fn(status_key1, None)
            # _run_phase leaves its final chunk's own time directory in place
            # (named latest1, its true cumulative iteration count) rather than
            # cleaning it itself - phase 1's own final state isn't meant for
            # standalone ParaView viewing (unlike phase 2's, kept below), so the
            # caller copies it into 0/ and, normally, cleans it away below. With
            # keep_all_timesteps, every phase-1 snapshot stays instead (phase 2's
            # _run_phase call below is offset by iters1 so its own directory names
            # continue the same numbering rather than colliding with phase 1's).
            #
            # Done BEFORE deciding whether to raise Phase1ExtrapolationUndecided -
            # _write_phase1_pending's docstring promises 0/ already holds this
            # attempt's current state for a later resume to build on, but that
            # was only true when accepted; if raising, 0/ was left however many
            # chunks stale it was from THIS SAME attempt (only non-final chunks
            # inside _run_phase's own loop copy their own latest to 0/) - a
            # further resume would then silently continue from that stale
            # point while reporting iteration counts as if it hadn't (confirmed
            # as a real gap on a live run, not just theoretical).
            _copy_latest_to_zero(case_dir_wsl, latest1, include_T=True, log_fn=log_fn)
            if phase1_extrapolation_gate and t_inf_rel_tol is not None and not stopped_via_tinf1:
                diagnostic = _phase1_extrapolation_diagnostic(
                    tinf_history1, t_inf_streak, t_inf_rel_tol, phase1_iterations,
                    t_inf_check_interval or phase1_iterations)
                raise Phase1ExtrapolationUndecided(
                    f"Phase 1's {phase1_iterations}-iteration ceiling was reached without a "
                    f"stable extrapolation - {diagnostic['summary']}", diagnostic, iters1)

            summary["phase1"] = _room_phase_summary(live1["room"], window_frac, converged1, iters1, t1, T1, log_fn)
            summary["phase1"]["mass_balance"] = windowed_mass_balance(
                live1["room"][0], mb_flux1["flow"], mb_flux1["weighted_t"], G, window_frac, mass_balance_tol)
            phase1_monitoring = {
                p["name"]: _point_phase_summary(live1[zone_name(p["name"])], window_frac)
                for p in (monitoring_points or [])
            }
            # Phase 2 is about to overwrite 0/T with its own (source + UV)
            # build-up state - keep a plain copy of Phase 1's converged field
            # under its own name (not inside a time directory - a stray extra
            # field there is otherwise harmless, but this keeps it unambiguous
            # and out of the way of anything mesh/time-directory-based) so a
            # spatial-uniformity analysis (see monitoring_points.
            # mixing_uniformity_note) can later be run against ventilation-only
            # mixing specifically, not conflated with Phase 2's highly non-
            # uniform UV-dose pattern (confirmed on a real run: even after
            # excluding the source cellZone and the most extreme 5% of cells,
            # Phase 2's spatial CV was still 213% - Phase 1 alone is the only
            # way to tell how much of that is ventilation mixing vs. UV dose).
            run_wsl_or_raise("cp 0/T phase1_T.snapshot", case_dir_wsl,
                              "saving Phase 1's final T field for later spatial-mixing analysis")
            if not keep_all_timesteps:
                _clean_time_dirs(case_dir_wsl)

            # Phase 1 is done and its converged state is safely in 0/ - write
            # the checkpoint now, before Phase 2 starts, so any failure from
            # here on can resume without repeating Phase 1 (see this run's own
            # real motivating case: Phase 1 converged fine, then an unrelated
            # directory-naming bug crashed the very next step).
            _clear_phase1_pending(case_dir)
            _write_phase1_checkpoint(case_dir, summary["phase1"], phase1_monitoring, G, Su, source_volume,
                                      n_source_cells)

    if phase1_only:
        return summary

    # --- Phase 2: source + UV ---
    log_fn("=== Phase 2: source + UV ===")
    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    uv_entries = _uv_fvoptions_entries(np.array(k_values), nbins)
    write_fvoptions_file(case_dir, [source_entry] + uv_entries + fan_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch: {n_open} vs {n_close}"

    # Phase 2 keeps T-infinity as an early-stop nicety only (not a hard
    # gate like Phase 1 - see phase1_extrapolation_gate) - stopped_via_tinf/
    # tinf_history/mass_balance_flux aren't meaningful for Phase 2 (mass
    # balance's injection=removal identity doesn't hold once UV sinks are
    # also removing T, see windowed_mass_balance's Phase-1-only caveat).
    status_key2 = f"Z={Z}/ACH={ach}/Phase2"
    try:
        latest2, iters2, t2, T2, converged2, live2, _, _, _ = _run_phase(
            case_dir, case_dir_wsl, phase2_iterations, phase2_write_interval,
            window_frac, plateau_rel_tol, log_fn, should_stop=should_stop,
            solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
            live_patches=patches_to_monitor,
            check_interval=t_inf_check_interval, t_inf_rel_tol=t_inf_rel_tol, t_inf_streak=t_inf_streak,
            keep_all_timesteps=keep_all_timesteps, iteration_offset=iters1,
            status_fn=status_fn, status_key=status_key2, should_pause=should_pause,
            delta_t=phase2_delta_t,
        )
    finally:
        if status_fn is not None:
            status_fn(status_key2, None)
    summary["phase2"] = _room_phase_summary(live2["room"], window_frac, converged2, iters2, t2, T2, log_fn)
    if monitoring_points:
        summary["monitoring"] = {
            p["name"]: {
                "phase1": phase1_monitoring[p["name"]],
                "phase2": _point_phase_summary(live2[zone_name(p["name"])], window_frac),
            }
            for p in monitoring_points
        }
    # Unlike phase 1, phase 2's final time directory is deliberately KEPT
    # (not cleaned) - it's the scenario's true final state, and a real
    # numbered directory (not just "0/") is what lets ParaView show it as
    # a proper timestep rather than the only entry in its time list.
    _copy_latest_to_zero(case_dir_wsl, latest2, include_T=True, log_fn=log_fn)

    lambda_vent = ach / 3600.0
    T_ss1, T_ss2 = summary["phase1"]["T_ss"], summary["phase2"]["T_ss"]

    # ACH/eACH_uv are a ratio of T_ss1/T_ss2 (see compute_corrected_eACH_uv's
    # docstring) - if either phase's curve hadn't fully flattened within its
    # iteration budget, the windowed average is a biased estimate of the
    # true steady state (confirmed on a real run: every windowed average
    # tried was ~3% off a well-fit exponential extrapolation - see
    # decay_analysis.fit_asymptotic_value), and that bias would propagate
    # straight into these derived numbers. Use the extrapolated T-infinity
    # instead whenever BOTH phases produced one; T_ss itself (the displayed
    # "moving average" row) is untouched either way.
    Tinf1 = summary["phase1"].get("T_inf_extrapolated")
    Tinf2 = summary["phase2"].get("T_inf_extrapolated")
    using_extrapolated = Tinf1 is not None and Tinf2 is not None
    T_ss1_ach, T_ss2_ach = (Tinf1, Tinf2) if using_extrapolated else (T_ss1, T_ss2)
    summary["ach_source"] = "extrapolated_T_infinity" if using_extrapolated else "windowed_average"
    if using_extrapolated:
        log_fn(f"  Using extrapolated T-infinity (T_ss1={T_ss1_ach:.4g}, T_ss2={T_ss2_ach:.4g}), "
               f"not the windowed average, for ACH/eACH_uv calculations below.")

    reduction_pct = (1 - T_ss2_ach / T_ss1_ach) * 100 if T_ss1_ach else None
    eACH_uv = lambda_vent * (T_ss1_ach / T_ss2_ach - 1) * 3600 if T_ss2_ach else None
    summary["reduction_pct"] = reduction_pct
    summary["eACH_uv_steady_state"] = eACH_uv
    log_fn(f"Reduction: {reduction_pct:.1f}%, eACH_uv (steady-state method) = {eACH_uv:.4g} /hr")

    if measured_ventilation_ach is not None:
        # Preferred: a real UV-off control run measured the ventilation
        # rate directly (see compute_corrected_eACH_uv_from_control's
        # docstring for why this is more reliable than Phase 1's own
        # T_ss1 whenever the source zone is small/localized). G_total/V
        # combined with this measured rate also implies a Phase-1 steady
        # state that isn't subject to that same mixing-transport-lag bias
        # - use it for reduction_pct too, not just eACH_uv.
        G_total = Su * source_volume
        T_ss1_corrected = G_total / (room_volume * (measured_ventilation_ach / 3600.0))
        eACH_uv_corrected = compute_corrected_eACH_uv_from_control(
            T_ss2_ach, Su, source_volume, room_volume, measured_ventilation_ach)
        reduction_pct_corrected = (1 - T_ss2_ach / T_ss1_corrected) * 100 if T_ss2_ach else None
        summary["ventilation_ach_measured"] = measured_ventilation_ach
        summary["eACH_uv_steady_state_corrected"] = eACH_uv_corrected
        summary["reduction_pct_corrected"] = reduction_pct_corrected
        summary["ventilation_measurement_method"] = "control_run"
        log_fn(f"  Measured ventilation ACH (from UV-off control run) = "
               f"{measured_ventilation_ach:.4g} /hr (nominal was {ach:.4g} /hr); "
               f"corrected eACH_uv = {eACH_uv_corrected:.4g} /hr, "
               f"corrected reduction = {reduction_pct_corrected:.1f}%")
    else:
        # Fallback: no control run available (e.g. the single-run path) -
        # derive the measured ventilation rate from Phase 1's own T_ss1
        # instead, same as before.
        ventilation_ach_measured, eACH_uv_corrected = compute_corrected_eACH_uv(
            T_ss1_ach, T_ss2_ach, Su, source_volume, room_volume)
        if ventilation_ach_measured is not None:
            summary["ventilation_ach_measured"] = ventilation_ach_measured
            summary["eACH_uv_steady_state_corrected"] = eACH_uv_corrected
            summary["ventilation_measurement_method"] = "phase1_buildup"
            log_fn(f"  Measured ventilation ACH (from Phase 1's own steady state) = "
                   f"{ventilation_ach_measured:.4g} /hr (nominal was {ach:.4g} /hr); "
                   f"corrected eACH_uv = {eACH_uv_corrected:.4g} /hr")

    run_wsl_or_raise("touch case.foam", case_dir_wsl, "touching case.foam")

    # The scenario finished end-to-end - no longer anything to resume, so
    # clear the checkpoint rather than leave a stale one sitting around
    # (harmless if left, since a finished case dir has results.json/
    # fluenceRate etc. too, but there's no reason to keep it either).
    _clear_phase1_checkpoint(case_dir)

    log_fn("Steady-state scenario complete.")
    return summary
