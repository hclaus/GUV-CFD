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
import re
import numpy as np

from .case_io import read_openfoam_scalar_field
from .cellzones import bin_decay_rates
from .contaminant_source import (
    write_source_topo_set_dict, compute_source_strength, source_Su, source_fvoptions_entry,
    write_fvoptions_file, check_mass_balance,
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
)
from .wsl_utils import wsl_path, run_wsl_or_raise, run_wsl_streaming, StoppedByUser


def compute_corrected_eACH_uv(T_ss1, T_ss2, Su, source_volume, room_volume):
    """Corrected eACH_uv using the *actual* ventilation removal rate instead
    of the nominal ACH - derived for free from Phase 1's own steady state,
    no separate UV-off control run needed (unlike the decay scenario).

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
    """write_interval must not exceed this chunk's own duration, or no
    snapshot ever lands within it and the post-chunk "did a new time
    directory appear" check fails a run that actually completed fine.

    controlDict's writeControl is "adjustableRunTime" (set once, in the
    template - set_control_dict_time() only ever rewrites endTime/
    writeInterval/deltaT's *values*, never writeControl itself) - unlike
    "timeStep" mode, this does not force a write at endTime, so a chunk
    shorter than the phase's normal write_interval writes nothing at all
    otherwise. Only matters for a short final/remainder chunk - normal
    full-size chunks are unaffected (chunk_size >= write_interval, so
    this is a no-op then). The T-infinity early-stop feature is what
    actually produces short final chunks in practice (whatever's left
    after dividing n_iterations into check_interval-sized pieces).
    """
    return min(write_interval, chunk_size)


def _copy_latest_to_zero(case_dir_wsl, latest, include_T, log_fn):
    fields = "U p k omega nut phi" + (" T" if include_T else "")
    r = run_wsl_or_raise(f"ls {latest}/", case_dir_wsl, "listing converged fields")
    available = set(r.stdout.split())
    to_copy = [f for f in fields.split() if f in available]
    log_fn(f"  Copying fields from {latest}/ to 0/: {to_copy}")
    cp_targets = " ".join(f"{latest}/{f}" for f in to_copy)
    run_wsl_or_raise(f"cp -f {cp_targets} 0/", case_dir_wsl, "copying converged fields")


def _run_phase(case_dir, case_dir_wsl, n_iterations, write_interval, window_frac, plateau_rel_tol,
                log_fn, should_stop=None, solver_log_fn=None, live_monitoring_zones=(),
                live_patches=(), check_interval=None, t_inf_rel_tol=None, t_inf_streak=3,
                keep_all_timesteps=False, iteration_offset=0):
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
    """
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
    live_block_names = ["volAverageLive1"] + [f"{p}AverageLive" for p in live_patches] \
        + [f"monitor_{z}Live" for z in live_monitoring_zones]
    with open(f"{case_dir}/system/controlDict") as f:
        already_spliced = "volAverageLive1" in f.read()
    if not already_spliced:
        block = live_vol_average_functions(
            field="T", patches=live_patches, monitoring_zones=live_monitoring_zones)
        _, n_open, n_close = splice_into_functions_block(case_dir, block)
        assert n_open == n_close, f"Brace mismatch after live-volAverage splice: {n_open} vs {n_close}"

    accumulated = {"room": ([], [])}
    for zone in live_monitoring_zones:
        accumulated[zone] = ([], [])
    tinf_history = []
    total_run = 0
    final_dir_name = None

    while total_run < n_iterations:
        chunk_size = min(check_interval, n_iterations - total_run)
        set_control_dict_time(case_dir, end_time=chunk_size,
                               write_interval=_chunk_write_interval(write_interval, chunk_size), delta_t=1)
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
            on_line=solver_log_fn or log_fn, should_stop=should_stop, kill_pattern="simpleFoam",
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
        # chunk's iterations - offset by total_run before appending, to
        # build one continuous global series across chunks.
        chunk_t, chunk_T = read_vol_average_dat(f"{case_dir}/postProcessing/volAverageLive1/0/volFieldValue.dat")
        acc_t, acc_T = accumulated["room"]
        acc_t.extend((chunk_t + total_run).tolist())
        acc_T.extend(chunk_T.tolist())
        for zone in live_monitoring_zones:
            zt, zT = read_vol_average_dat(f"{case_dir}/postProcessing/monitor_{zone}Live/0/volFieldValue.dat")
            azt, azT = accumulated[zone]
            azt.extend((zt + total_run).tolist())
            azT.extend(zT.tolist())

        total_run += chunk_size
        chunk_start = total_run - chunk_size

        stop_early = False
        if t_inf_rel_tol is not None and total_run < n_iterations:
            fit = fit_asymptotic_value(np.array(acc_t), np.array(acc_T))
            tinf_history.append(fit["Tinf"] if fit else None)
            if check_t_infinity_stability(tinf_history, rel_tol=t_inf_rel_tol, streak=t_inf_streak):
                log_fn(f"  T-infinity stable ({t_inf_streak}x within {t_inf_rel_tol:.0%}) - "
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
    return final_dir_name, total_run, sparse_t, sparse_T, converged, live_curves


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
                               fan_entry=None, monitoring_points=None,
                               patches_to_monitor=("outlet",), log_fn=print, should_stop=None,
                               solver_log_fn=None):
    """Run both phases of a continuous-source steady-state scenario against
    an already-converged case (mesh + flow + fluenceRate/kUV must already
    exist - see run_pipeline.setup_case()). Returns a summary dict.

    mass_balance_tol: fractional tolerance for Phase 1's mass-balance check
    (contaminant_source.check_mass_balance) - compares the actual outlet
    removal rate against the known injection rate G, a curve-fitting-free
    convergence signal (at true steady state they must match exactly).
    Phase-1-only: Phase 2 also removes T via the UV sink cellZones, not
    just advective outflow, so the same simple injection=removal identity
    doesn't hold there (see check_mass_balance's docstring).

    t_inf_check_interval/t_inf_rel_tol/t_inf_streak: optional early-stop
    for both phases via T-infinity extrapolation stability (see
    _run_phase/decay_analysis.check_t_infinity_stability) - t_inf_rel_tol
    is None by default (disabled; each phase always runs its full
    phaseN_iterations budget, today's behavior). GUI-exposed as a
    cross-project "advanced" setting (Settings menu, right of File).

    keep_all_timesteps: if True, every write_interval snapshot from both
    phases is kept on disk (renamed to one continuous, collision-free
    cumulative iteration count spanning phase 1 then phase 2) instead of
    being deleted down to just the initial/final state - lets ParaView
    play back the whole run. Off by default: a long/fine-grained run can
    leave a lot of snapshot directories behind, so this is opt-in.

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
    """
    case_dir_wsl = wsl_path(case_dir)
    room_volume = room_x * room_y * room_z
    if source_center is None:
        source_center = (room_x / 2, room_y / 2, 1.6)
    summary = {"room_volume": room_volume, "source_center": source_center, "target_T_ss": target_T_ss}

    run_wsl_or_raise("touch case.foam", case_dir_wsl, "touching case.foam")

    log_fn("Ensuring SIMPLE fvSolution and outlet-average monitoring are set up...")
    ensure_simple_fvsolution(case_dir)
    write_vol_average_dict(case_dir, field="T", patches=patches_to_monitor)

    # Carve monitoring cellZones now, before either phase's solve, instead
    # of a post-hoc pass after each phase - topoSet is mesh-only (not
    # field-dependent), and the mesh is fixed for the rest of this
    # scenario, so the zones carved here stay valid for both phase 1 and
    # phase 2's live function objects.
    live_zone_names = []
    if monitoring_points:
        write_monitoring_topo_set_dict(case_dir, monitoring_points, cell_size)
        run_wsl_or_raise("topoSet -dict system/monitoringTopoSetDict", case_dir_wsl,
                          "topoSet (monitoring zones)")
        live_zone_names = [zone_name(p["name"]) for p in monitoring_points]

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
    # starts by explicitly rewriting boundary conditions again (T_initial=0
    # for the steady-state build-up) - re-resolve the same way here rather
    # than assuming the plain inlet_velocity tuple this function received
    # is still the right BC value for a "ceiling" inlet.
    if inlet_diffuser_type == "ceiling":
        v_mag = float(np.linalg.norm(inlet_velocity))
        center = opening_center(inlet_wall, room_x, room_y, room_z, inlet_center, inlet_size, cell_size=cell_size)
        extents = opening_half_extents(inlet_wall, room_x, room_y, room_z, inlet_center, inlet_size,
                                        cell_size=cell_size)
        inlet_velocity = resolve_inlet_velocity(case_dir, "inlet", inlet_wall, center, v_mag, "ceiling",
                                                 half_extents=extents)
    if inlet2_diffuser_type == "ceiling" and inlet2_velocity is not None:
        v_mag2 = float(np.linalg.norm(inlet2_velocity))
        center2 = opening_center(inlet2_wall, room_x, room_y, room_z, inlet2_center, inlet2_size, cell_size=cell_size)
        extents2 = opening_half_extents(inlet2_wall, room_x, room_y, room_z, inlet2_center, inlet2_size,
                                         cell_size=cell_size)
        inlet2_velocity = resolve_inlet_velocity(case_dir, "inlet2", inlet2_wall, center2, v_mag2, "ceiling",
                                                  half_extents=extents2)

    # --- Phase 1: source only, no UV ---
    log_fn("=== Phase 1: source only (no UV) ===")
    write_fvoptions_file(case_dir, [source_entry] + fan_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch: {n_open} vs {n_close}"
    # Warm-start T at target_T_ss rather than 0: this is a linear system (T
    # doesn't feed back into U/p), so the final steady state doesn't depend
    # on the initial condition at all - only how many iterations it takes to
    # get there does. target_T_ss is exactly what the source strength was
    # calibrated to reach under the idealized well-mixed assumption (see
    # compute_source_strength), so it's a good guess to start near rather
    # than the full climb from 0 - confirmed on a real case that starting
    # near the eventual answer reaches a tight, guard-passing plateau in a
    # small fraction of the iterations a T=0 start needed for the same
    # curve. Phase 2 already does the equivalent right thing (starts from
    # Phase 1's own converged T); this brings Phase 1 in line with that.
    restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, T_initial=target_T_ss,
                                 inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2)

    latest1, iters1, t1, T1, converged1, live1 = _run_phase(
        case_dir, case_dir_wsl, phase1_iterations, phase1_write_interval,
        window_frac, plateau_rel_tol, log_fn, should_stop=should_stop,
        solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
        live_patches=patches_to_monitor,
        check_interval=t_inf_check_interval, t_inf_rel_tol=t_inf_rel_tol, t_inf_streak=t_inf_streak,
        keep_all_timesteps=keep_all_timesteps,
    )
    summary["phase1"] = _room_phase_summary(live1["room"], window_frac, converged1, iters1, t1, T1, log_fn)
    summary["phase1"]["mass_balance"] = check_mass_balance(
        case_dir, patches_to_monitor, G, tol=mass_balance_tol, log_fn=log_fn)
    # _run_phase leaves its final chunk's own time directory in place
    # (named latest1, its true cumulative iteration count) rather than
    # cleaning it itself - phase 1's own final state isn't meant for
    # standalone ParaView viewing (unlike phase 2's, kept below), so the
    # caller copies it into 0/ and, normally, cleans it away here. With
    # keep_all_timesteps, every phase-1 snapshot stays instead (phase 2's
    # _run_phase call below is offset by iters1 so its own directory names
    # continue the same numbering rather than colliding with phase 1's).
    _copy_latest_to_zero(case_dir_wsl, latest1, include_T=True, log_fn=log_fn)
    if not keep_all_timesteps:
        _clean_time_dirs(case_dir_wsl)

    # --- Phase 2: source + UV ---
    log_fn("=== Phase 2: source + UV ===")
    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    uv_entries = _uv_fvoptions_entries(np.array(k_values), nbins)
    write_fvoptions_file(case_dir, [source_entry] + uv_entries + fan_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch: {n_open} vs {n_close}"

    latest2, iters2, t2, T2, converged2, live2 = _run_phase(
        case_dir, case_dir_wsl, phase2_iterations, phase2_write_interval,
        window_frac, plateau_rel_tol, log_fn, should_stop=should_stop,
        solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
        live_patches=patches_to_monitor,
        check_interval=t_inf_check_interval, t_inf_rel_tol=t_inf_rel_tol, t_inf_streak=t_inf_streak,
        keep_all_timesteps=keep_all_timesteps, iteration_offset=iters1,
    )
    summary["phase2"] = _room_phase_summary(live2["room"], window_frac, converged2, iters2, t2, T2, log_fn)
    if monitoring_points:
        summary["monitoring"] = {
            p["name"]: {
                "phase1": _point_phase_summary(live1[zone_name(p["name"])], window_frac),
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

    # Corrected eACH_uv using the actual (not nominal) ventilation removal
    # rate - see compute_corrected_eACH_uv's docstring. Unlike the decay
    # scenario, this is free: no separate UV-off control run needed.
    ventilation_ach_measured, eACH_uv_corrected = compute_corrected_eACH_uv(
        T_ss1_ach, T_ss2_ach, Su, source_volume, room_volume)
    if ventilation_ach_measured is not None:
        summary["ventilation_ach_measured"] = ventilation_ach_measured
        summary["eACH_uv_steady_state_corrected"] = eACH_uv_corrected
        log_fn(f"  Measured ventilation ACH (from Phase 1's own steady state) = "
               f"{ventilation_ach_measured:.4g} /hr (nominal was {ach:.4g} /hr); "
               f"corrected eACH_uv = {eACH_uv_corrected:.4g} /hr")

    run_wsl_or_raise("touch case.foam", case_dir_wsl, "touching case.foam")

    log_fn("Steady-state scenario complete.")
    return summary
