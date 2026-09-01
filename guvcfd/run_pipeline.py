"""Run the full OpenFOAM case setup pipeline end-to-end from a single call:
mesh generation, OpenFOAM binary invocations (blockMesh/topoSet/createPatch/
checkMesh/writeCellCentres/mapFields), fluence/k computation, cellZones/
fvOptions binning, and fvOptions splicing - given just a .guv project and a
target case directory. Everything else this package's modules do piecewise
via manual WSL shell-outs, this ties into one command.
"""
import contextlib
import json
import re
import time
from pathlib import Path

import numpy as np

from guv_calcs import Project

from .case_io import read_cell_centers, read_cell_volumes, read_boundary_patch_names, write_scalar_field
from .cellzones import bin_decay_rates, write_cellzones, write_fvoptions
from .contaminant_source import (write_fvoptions_file, source_box_grid_alignment,
                                  resolve_source_size, suggest_source_center_fix)
from .decay_analysis import read_vol_average_dat
from .fan import write_fan_topo_set_dict, fan_fvoptions_entry
from .fluence import compute_fluence_at_points, compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import (
    write_initial_fields, compute_inlet_velocity, restore_boundary_conditions, resolve_inlet_velocity,
)
from .mesh_gen import (
    write_mesh_dicts, write_map_fields_dict, opening_center, opening_half_extents, opening_grid_alignment,
    opening_actual_area, write_decompose_par_dict, suggest_opening_size_fix, suggest_opening_center_fix,
    write_lamp_refine_topo_set_dict, write_opening_refine_topo_set_dict, write_refine_mesh_dict,
    _opening_refine_box,
)
from .monitoring import write_vol_average_dict
from .visualization import center_frac_for_wall
from .splice import (
    splice_fv_options_into_control_dict,
    set_function_object_enabled,
    set_control_dict_time,
    ensure_simple_fvsolution,
    set_lts_ddt_scheme,
    set_relaxation_factors,
    compute_adaptive_scalar_relaxation,
    set_scalar_transport_correction,
    set_pressure_reference_cell,
)
from .wsl_utils import (
    wsl_path as _wsl_path,
    windows_path_to_wsl_mnt as _windows_path_to_wsl_mnt,
    ensure_case_subdir as _ensure_case_subdir,
    write_case_file as _write_case_file,
    read_case_file as _read_case_file,
    run_wsl as _run_wsl,
    run_wsl_or_raise as _run_wsl_or_raise,
    run_wsl_streaming as _run_wsl_streaming,
    StoppedByUser,
)


class FlowConvergenceUndecided(Exception):
    """Raised by converge_flow_field() when max_iterations is exhausted
    without a clear verdict (neither "converged" nor "accepted bounded
    oscillation") - deliberately NOT a bare RuntimeError: this is an
    expected outcome that needs a human decision, not a crash. Carries
    everything a caller needs to present that decision (see `diagnostic`)
    and to resume (via continue_flow_convergence()) without redoing any
    of the expensive mesh/flow-field work already on disk.

    Distinct from a genuine solver failure (FOAM FATAL, non-zero exit,
    StoppedByUser) - those still raise RuntimeError/StoppedByUser as
    before, since there's nothing a "continue more iterations" choice
    could do about an actual crash.
    """

    def __init__(self, message, diagnostic, total_iterations):
        super().__init__(message)
        self.diagnostic = diagnostic
        self.total_iterations = total_iterations


_HISTORY_RELATIVE_PATH = "flow_convergence_history.json"


def _load_history(case_dir):
    """The persisted per-chunk volAverage(check_field) history from a prior
    converge_flow_field()/continue_flow_convergence() call in this case
    directory, or [] if there isn't one (a fresh case, or one that never
    got far enough to write it). Persisted (not just kept in memory, as
    before) specifically so a run that stops - for any reason, including
    the process itself going away - leaves enough on disk to (a) diagnose
    what actually happened and (b) resume without losing the oscillation-
    acceptance check's accumulated evidence.

    Read/written via _read_case_file/_write_case_file (WSL-native for a
    real case_dir), not plain Windows-side open() - confirmed directly
    2026-08-03: a genuine multi-hour flow convergence lost its entire
    resumability this way (every _save_history() call "succeeded" with no
    exception, but the file was never actually durable to WSL, so a crash
    elsewhere left _load_history() seeing nothing at all - see
    wsl_utils.write_wsl_text's docstring for why).
    """
    try:
        return json.loads(_read_case_file(case_dir, _HISTORY_RELATIVE_PATH))
    except (json.JSONDecodeError, OSError, RuntimeError):
        return []


def _save_history(case_dir, history):
    _write_case_file(case_dir, _HISTORY_RELATIVE_PATH, json.dumps(history, indent=2))


def _oscillation_diagnostic(history, window, growth_tol, rel_tol, n_iterations, check_field,
                             converged_chunks=3):
    """Best-effort analysis of whatever chunk history actually exists,
    regardless of whether there's enough of it for the formal bounded-
    oscillation check (_is_stable_oscillation) to reach a verdict - the
    real problem this fixes: that check silently returning False for
    "not enough evidence yet" and for "genuinely still growing" look
    identical from the outside otherwise, and conflating them produced a
    real, misleading failure (see FlowConvergenceUndecided's docstring
    motivation - a run capped at 10 chunks with oscillation_window=6
    needed 12 to ever be evaluated, and never was, despite an error
    message implying it had been).

    Returns a dict with the raw numbers a caller needs to build its own
    decision UI, plus a `summary` plain-language string.
    """
    values = [h["value"] for h in history]
    chunks_available = len(values)
    chunks_needed = 2 * window
    insufficient_history = chunks_available < chunks_needed

    bounded = None
    trend = "not enough data yet"
    if chunks_available >= 2:
        half = chunks_available // 2
        older_mean = sum(values[:half]) / half
        newer_mean = sum(values[half:]) / (chunks_available - half)
        # Guard divide-by-zero for a field that's genuinely averaging to 0.
        rel_drift = abs(newer_mean - older_mean) / abs(older_mean) if older_mean else abs(newer_mean - older_mean)
        if rel_drift <= max(rel_tol, 0.02):
            trend = "flat"
        elif newer_mean > older_mean:
            trend = "still rising"
        else:
            trend = "still falling"
    if not insufficient_history:
        bounded = _is_stable_oscillation(values, window, growth_tol)

    last_rel_change = None
    if len(values) >= 2 and values[-2]:
        last_rel_change = abs(values[-1] - values[-2]) / abs(values[-2])

    # How much of a converged streak has actually accumulated. Far more
    # informative than last_chunk_rel_change on its own, which is small at
    # every turning point of an oscillation and so says nothing by itself.
    converged_streak = 0
    for i in range(len(values) - 1, 0, -1):
        if not values[i - 1] or abs(values[i] - values[i - 1]) / abs(values[i - 1]) > rel_tol:
            break
        converged_streak += 1

    if insufficient_history:
        summary = (
            f"Not enough chunk history yet to tell whether this is a stable, bounded "
            f"oscillation (needs {chunks_needed} chunks of evidence, only {chunks_available} "
            f"available) - this is NOT the same as having checked and found it unstable. "
            f"The trend so far is {trend}."
        )
    elif bounded:
        summary = (
            f"Enough history to check ({chunks_available} chunks), and it looks like a "
            f"stable, bounded oscillation - not growing or drifting further. Downstream "
            f"results are typically insensitive to exactly which point in the cycle gets "
            f"used, so accepting this as-is is usually reasonable."
        )
    else:
        summary = (
            f"Enough history to check ({chunks_available} chunks), and the amplitude is "
            f"still GROWING or drifting rather than settling - trend: {trend}. This looks "
            f"like a genuine non-convergence issue, not just an oscillating-but-stable flow; "
            f"accepting it as-is is not recommended."
        )

    return {
        "chunks_available": chunks_available,
        "chunks_needed_for_oscillation_check": chunks_needed,
        "insufficient_history": insufficient_history,
        "bounded": bounded,
        "trend": trend,
        "last_chunk_rel_change": last_rel_change,
        "converged_streak": converged_streak,
        "converged_chunks_required": converged_chunks,
        "rel_tol": rel_tol,
        "oscillation_window": window,
        "oscillation_growth_tol": growth_tol,
        "chunk_size": n_iterations,
        "check_field": check_field,
        "recent_values": values[-chunks_needed:] if chunks_available else [],
        "summary": summary,
    }


def _is_stable_oscillation(history, window, growth_tol):
    """True if the last `window` chunk values oscillate within a range that
    isn't growing (or drifting) compared to the `window` chunks before that -
    i.e. genuinely bounded turbulent unsteadiness (an impinging jet/fan hitting
    a wall never settles to a single value, but keeps cycling through roughly
    the same range) rather than a still-settling or diverging flow field.
    Needs at least 2*window chunks of history to make the comparison
    meaningful; returns False otherwise (safe default - keep the hard failure
    when there isn't enough evidence either way).
    """
    if len(history) < 2 * window:
        return False
    older, newer = history[-2 * window:-window], history[-window:]
    old_amp, new_amp = max(older) - min(older), max(newer) - min(newer)
    if old_amp == 0 and new_amp == 0:
        return True
    if new_amp > growth_tol * old_amp:
        return False
    drift = abs(sum(newer) / len(newer) - sum(older) / len(older))
    return drift <= max(new_amp, old_amp)


def converge_flow_field(case_dir, n_iterations=500, fan_entry=None, log_fn=print,
                         max_iterations=20000, check_field="p", rel_tol=0.01, should_stop=None,
                         method="simple", oscillation_window=6, oscillation_growth_tol=1.5,
                         converged_chunks=3,
                         solver_log_fn=None, resume=False, should_pause=None, skip_potential_flow=False,
                         n_procs=None, solve_semaphore=None):
    """Run simpleFoam to actually converge the flow field on this mesh,
    starting from whatever is in 0/ (e.g. a mapFields warm start), then copy
    the result back into 0/ so it becomes pimpleFoam's starting point.

    mapFields alone only gives an interpolated *initial guess* - it doesn't
    verify or produce a converged solution for this mesh's specific topology.
    scalarTransport1 is temporarily disabled (see
    splice.set_function_object_enabled's docstring - running the UV-decay
    scalar transport against a wildly unconverged early flow field crashes
    with a floating-point exception), and simpleFoam gets its own iteration
    budget via controlDict's endTime/deltaT/writeInterval, separate from
    whatever pimpleFoam's transient duration is set to (iterations and
    physical seconds are different things, even though both solvers read
    the same controlDict).

    Any solver (not just via the scalarTransport function object) also
    auto-loads constant/fvOptions directly if it exists - if this is called
    before fluence/cellZones/fvOptions have been (re)generated for the
    current mesh, a stale fvOptions from a previous run on a *different*
    mesh will reference cellZones that don't exist yet, and simpleFoam fails
    immediately. constant/fvOptions is normally meaningless during flow
    development (the UV/source sink terms target "T", which simpleFoam
    doesn't even solve), so it's removed here rather than reordering the
    whole pipeline - *except* fan_entry (see fan.py's meanVelocityForce
    entry), which acts on U directly and so is relevant during flow
    convergence too: a real fan affects the converged flow field itself,
    not just the later scalar-transport phases.

    Convergence is checked directly rather than trusted from fvSolution's
    own SIMPLE{residualControl{}} (p/U/k/omega at 1e-4): empirically, on
    these room-ventilation meshes, residuals plateau around 1e-2/1e-3 - well
    above that threshold - and never trigger simpleFoam's own early stop,
    even once the flow field itself has stopped changing physically. So
    instead this runs in n_iterations-sized chunks, and after each chunk
    compares the room's volume-averaged `check_field` (a representative
    scalar flow quantity - p by default) against the previous chunk's value;
    once the relative change has stayed <= rel_tol for `converged_chunks`
    CONSECUTIVE chunks, the flow field is accepted as converged. Capped at
    max_iterations total to avoid a runaway on a case that never plateaus.

    The consecutive requirement is the whole point of that test, not a
    refinement of it. A single chunk-to-chunk change is small exactly where
    the signal's slope passes through zero, which on an oscillating quantity
    is a TURNING POINT - so a one-sample test fires preferentially at the
    extremes of the cycle, and reports "converged" for a field that is still
    swinging by tens of percent. Two real runs (patient ward v9 and V10) were
    accepted this way off a single lucky chunk out of 15 and 7 respectively,
    each landing on an extreme of its own series (V10 on its maximum, +24%
    off the series mean; v9 second-lowest of 16, -18% off). A genuinely
    settling flow produces a RUN of small changes; a turning point produces
    exactly one.

    The bounded-oscillation check below is also applied inside the chunk
    loop, not only after the budget is exhausted: a genuinely turbulent flow
    never produces a run of small changes, so requiring one would otherwise
    spend the entire max_iterations budget only to reach the same verdict
    the oscillation test could have reached much earlier.

    If max_iterations is reached without converging, this doesn't
    unconditionally fail: some flows (e.g. a fan jet impinging directly on a
    wall/floor) are genuinely, persistently unsteady and will never satisfy
    rel_tol no matter how long simpleFoam runs - that's real turbulence, not
    a numerical tuning problem. So the last 2*oscillation_window chunks are
    checked for *bounded* oscillation (see _is_stable_oscillation): if the
    swing in volAverage(check_field) over the most recent oscillation_window
    chunks isn't growing (nor drifting) relative to the oscillation_window
    chunks before that, the field is accepted as-is. This was verified
    empirically (not just assumed): two flow-field snapshots frozen 500
    iterations apart during exactly this kind of bounded oscillation
    produced eACH_uv_effective within ~2% of each other, so which point in
    the cycle the field gets frozen at doesn't meaningfully affect the
    downstream scalar-decay result.

    If neither converged nor accepted (still trending/growing, OR there
    isn't yet enough chunk history to tell the two cases apart -
    genuinely different situations that used to be conflated into the same
    unconditional failure, see FlowConvergenceUndecided's docstring), this
    raises FlowConvergenceUndecided rather than a bare RuntimeError - an
    expected outcome needing a human decision (continue further, or accept
    as-is), not a crash. A genuine solver failure (FOAM FATAL, non-zero
    exit) still raises RuntimeError as before.

    resume: True to continue a case directory whose flow convergence
    previously stopped without a verdict (typically after catching
    FlowConvergenceUndecided and choosing "continue" - see
    continue_flow_convergence()) - loads the persisted chunk history from
    disk instead of starting a fresh one (so the oscillation-acceptance
    check keeps whatever evidence already accumulated, rather than its
    2*oscillation_window-chunk clock restarting from zero), and skips
    potentialFoam (which would overwrite the existing, already-developed p
    field with a fresh irrotational guess - actively counterproductive for
    a warm continuation, unlike a genuine cold start). Everything else
    (fvOptions, SIMPLE{} block, LTS switch) is idempotent and safe to redo
    on a resume, so isn't skipped.

    method: "simple" (default) runs simpleFoam under plain SIMPLE/SIMPLEC.
    "lts" runs pimpleFoam under Local Time Stepping (ddtSchemes.default =
    localEuler, see splice.set_lts_ddt_scheme) - each cell gets its own
    pseudo-timestep sized to its own local Courant number, which can
    converge much faster than uniform-step SIMPLE for flows with very
    different length/time scales in different regions (e.g. a fast fan jet
    next to otherwise-still air). The ddtScheme is always restored back to
    Euler before returning (success, failure, or stop) - the later
    transient (real time-accurate) pimpleFoam decay run needs that, not LTS.

    solver_log_fn: on_line callback for simpleFoam/pimpleFoam's raw stdout
    (a few lines per iteration, thousands of lines over a full convergence
    run) - defaults to log_fn if not given, but callers with a visible run
    log generally want a quieter callback here so per-iteration solver
    chatter doesn't flood it (log_fn's own chunk-boundary narration is
    unaffected either way).

    should_pause: forwarded straight to run_wsl_streaming - suspends the
    active solver process in place (no iterations lost, no exception
    raised) instead of killing it, unlike should_stop.

    skip_potential_flow: True to skip potentialFoam even on a genuine cold
    start (independent of `resume`) - for a sealed (zero-ACH) case, EVERY
    patch is a zero-flux wall, so potentialFoam's velocity-potential solve
    is fully degenerate (a pure-Neumann Laplace problem with zero RHS
    everywhere) and its "approximate pressure field" step reliably crashes
    with a floating point exception (confirmed: this is what a bare ACH=0
    run used to hit). potentialFoam also ignores fvOptions entirely, so it
    can't see a sealed case's only real driving force (the fan) anyway -
    there's nothing useful for it to compute here, just risk.

    n_procs: None (default) runs the solver serially, exactly as before.
    An integer N decomposes the mesh into N subdomains (decomposeParDict,
    scotch method - no geometric coefficients needed) once up front, then
    every chunk: re-decomposes only the current 0/ field data (fast -
    `decomposePar -fields`, not a full mesh redecomposition, since only
    the fields change between chunks, not the mesh), runs
    `mpirun -np N {solver} -parallel`, and reconstructs back into a single
    serial time directory (`reconstructPar -latestTime`) before returning
    control to the rest of this loop - every downstream step (volAverage
    read, field copy-back to 0/, cleanup) then sees an ordinary serial
    case directory, unchanged from the n_procs=None path. Independent of
    `method` - "simple"+parallel and "lts"+parallel are both valid
    combinations. Untested against a real solve as of the day this was
    added - verify on a real case before trusting it for anything that
    matters.

    solve_semaphore: if given, acquired only around each chunk's own
    solver invocation below (not held across the whole convergence loop,
    and never held while merely waiting on something else) - lets a
    caller cap how many REAL concurrent OpenFOAM processes are running
    across a whole sweep, decoupled from how many orchestration/waiting
    threads exist (see scenario_runs.py's own module docstring for the
    starvation bug this closes: a thread pool sized to the same number
    used to also gate real solve concurrency let threads that were merely
    blocked waiting on an unrelated future silently eat a pool slot for
    free, starving other ACH groups' work that had nothing left to wait
    on). None (default) behaves exactly as before - unlimited.
    """
    case_dir_wsl = _wsl_path(case_dir)
    solver = "pimpleFoam" if method == "lts" else "simpleFoam"
    solver_cmd = f"mpirun -np {n_procs} {solver} -parallel" if n_procs else solver

    if not resume:
        # A cold start must never trust a time directory it didn't just
        # write itself - confirmed as a real incident: re-running flow
        # convergence on a case_dir that had already been through a full
        # Phase 1/Phase 2 (steady_state_pipeline's own chunk loop
        # deliberately KEEPS its intermediate time directories, unlike this
        # loop's own end-of-chunk cleanup below) left a stale "5000/"
        # sitting there from that earlier, unrelated run. The very first
        # chunk here wrote a genuine fresh "500/", but the "latest time
        # directory" check further down used to trust whichever number was
        # numerically largest, not whichever one this chunk actually wrote -
        # so it found the stale "5000/" and concluded the solver had
        # overrun. Below, that check was also hardened to look for the
        # exact expected directory instead of the largest one, but a stale
        # directory sharing an actual future chunk's name (this loop's own
        # chunk_end could legitimately reach 5000 too) risks simpleFoam or
        # the cp/rm steps interacting with leftover foreign field data, not
        # just confusing the check - so it's cleared here too, not left for
        # this loop's own per-chunk cleanup to eventually catch up with.
        _run_wsl_or_raise('for d in [0-9]*/; do [ "$d" = "0/" ] || rm -rf "$d"; done',
                           case_dir_wsl, "clearing stale time directories from an earlier run")

    if n_procs:
        log_fn(f"Decomposing mesh into {n_procs} subdomains for parallel solving (scotch method)...")
        write_decompose_par_dict(case_dir, n_procs)
        _run_wsl_or_raise("decomposePar -force", case_dir_wsl, "decomposePar")

    log_fn(f"Flow-convergence budget: {max_iterations} iterations max, in chunks of {n_iterations}...")

    log_fn("Disabling scalarTransport1 for flow development...")
    set_function_object_enabled(case_dir, "scalarTransport1", False)

    if fan_entry is not None:
        log_fn("Writing fan-only constant/fvOptions (kept active during flow "
               "convergence, unlike the UV/source entries)...")
        write_fvoptions_file(case_dir, [fan_entry])
    else:
        log_fn("Removing any stale constant/fvOptions (solvers auto-load it if present, "
               "and it's meaningless during flow-only development with no fan)...")
        _run_wsl("rm -f constant/fvOptions", case_dir_wsl)

    log_fn("Ensuring fvSolution has a SIMPLE{} block with under-relaxation "
           "(the reference case's fvSolution was only ever set up for PIMPLE)...")
    ensure_simple_fvsolution(case_dir)

    if method == "lts":
        log_fn("Switching to Local Time Stepping (ddtSchemes.default = localEuler) "
               "for pseudo-transient flow convergence via pimpleFoam...")
        set_lts_ddt_scheme(case_dir, True)

    if resume or skip_potential_flow:
        if resume:
            log_fn("Resuming: skipping potentialFoam (would overwrite the existing, "
                   "already-developed flow field with a fresh irrotational guess - only "
                   "wanted for a genuine cold start)...")
        else:
            log_fn("Sealed case (ACH=0): skipping potentialFoam - every patch is a wall, "
                   "so its irrotational solve is fully degenerate and it ignores the fan "
                   "anyway. Starting simpleFoam from the uniform-zero initial guess instead.")
    else:
        log_fn("Running potentialFoam for a better initial guess than uniform-zero "
               "(cheap inviscid/irrotational solve, skips most of the 'spin up from "
               "nothing' phase simpleFoam would otherwise need)...")
        r = _run_wsl("potentialFoam -writep", case_dir_wsl)
        if r.returncode != 0:
            log_fn(f"  potentialFoam failed (exit {r.returncode}) - continuing from the "
                    f"uniform-zero initial guess instead, this is an optimization, not "
                    f"a requirement:\n{(r.stdout + r.stderr)[-1500:]}")
        else:
            log_fn("  potentialFoam initial guess written.")

    log_fn(f"Writing flow-convergence monitor (room volume-average {check_field})...")
    write_vol_average_dict(case_dir, field=check_field, patches=())

    if resume:
        history = _load_history(case_dir)
        total_run = history[-1]["iteration"] if history else 0
        prev_avg = history[-1]["value"] if history else None
        log_fn(f"Resuming from {total_run} iterations total, with {len(history)} chunks of "
               f"persisted history...")
    else:
        history = []
        total_run = 0
        prev_avg = None
        _save_history(case_dir, history)
    converged = False
    accepted_oscillation = False
    # Consecutive chunks whose change stayed within rel_tol. A settling flow
    # produces a RUN of small changes; an oscillating one produces exactly one
    # (at each turning point, where the chunk-to-chunk slope passes through
    # zero) - which is why a single small change is not evidence of anything.
    # Recomputed from persisted history on a resume, so a continuation isn't
    # forced to re-earn a streak it had already accumulated.
    streak = 0
    _vals = [h["value"] for h in history]
    for _i in range(len(_vals) - 1, 0, -1):
        if not _vals[_i - 1] or abs(_vals[_i] - _vals[_i - 1]) / abs(_vals[_i - 1]) > rel_tol:
            break
        streak += 1

    try:
        while total_run < max_iterations:
            chunk_end = total_run + n_iterations
            log_fn(f"Running {solver} iterations {total_run + 1}-{chunk_end} "
                   f"(chunk size {n_iterations})...")
            set_control_dict_time(case_dir, end_time=n_iterations, write_interval=n_iterations, delta_t=1)
            # Verify the write actually landed before launching the solver -
            # confirmed directly (2026-08-04) that set_control_dict_time's
            # own WSL-native write can still, in the REAL chunk-loop
            # sequence (not reproducible in isolation), leave a
            # subsequently-launched separate WSL process seeing a stale
            # endTime - exact mechanism not pinned down despite real
            # investigation, but this closes the gap regardless of cause,
            # which matters more given how expensive this exact failure
            # mode already was today (see the safety-net check just below,
            # added earlier the same day for the same reason).
            for attempt in range(5):
                r = _run_wsl('grep -oE "endTime[[:space:]]+[0-9.]+" system/controlDict | '
                             'grep -oE "[0-9.]+$"', case_dir_wsl)
                if r.stdout.strip() == str(n_iterations):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"set_control_dict_time wrote endTime={n_iterations} but it never became visible "
                    f"after 5 retries (last read: {r.stdout.strip()!r}) - not launching {solver} against "
                    f"a controlDict that may still have a stale endTime.")

            if n_procs:
                # Only the field data changes between chunks (mesh is fixed,
                # decomposed once above) - `-fields` re-decomposes just 0/
                # onto the existing processorN/ mesh split, far cheaper than
                # a full redecomposition every chunk.
                _run_wsl_or_raise("decomposePar -fields -time 0 -force", case_dir_wsl,
                                   "decomposePar (fields)")

            with solve_semaphore or contextlib.nullcontext():
                r = _run_wsl_streaming(
                    f"{solver_cmd} 2>&1 | tee log.{solver}", case_dir_wsl,
                    on_line=solver_log_fn or log_fn, should_stop=should_stop, kill_pattern=solver,
                    should_pause=should_pause,
                )
            if should_stop is not None and should_stop():
                raise StoppedByUser("Stopped during flow convergence.")
            if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
                tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
                raise RuntimeError(f"{solver} failed (exit {r.returncode}):\n{tail}")

            if n_procs:
                log_fn("  Reconstructing parallel result into a single serial time directory...")
                _run_wsl_or_raise("reconstructPar -latestTime", case_dir_wsl, "reconstructPar")

            # Every chunk restarts from "0/" (holding the previous chunk's
            # results, relabeled) and runs to endTime=n_iterations again (set
            # just above), by design, so the ONLY directory this chunk could
            # legitimately have written is one named exactly n_iterations -
            # checked directly by existence, not by "whichever numbered
            # directory is largest" (that used to falsely flag a genuine,
            # freshly-written chunk as having overrun when a stale, larger-
            # numbered directory from an earlier, unrelated run of this same
            # case_dir was still sitting around - see the cold-start cleanup
            # above, which closes the common case, but this check stays
            # exact regardless). (Earlier version of this code derived
            # total_run from the largest directory directly, on the wrong
            # assumption that endTime accumulated across chunks like
            # chunk_end does - it doesn't; that broke real progress
            # tracking, since the chunk's own directory legitimately stays
            # at n_iterations forever.)
            latest = str(n_iterations)
            r = _run_wsl_or_raise(f'[ -d "{latest}" ] && echo yes || echo no',
                                   case_dir_wsl, "checking for this chunk's expected time directory")
            if r.stdout.strip() != "yes":
                raise RuntimeError(
                    f"{solver} did not write the expected time directory {latest}/ - "
                    f"endTime write may not have taken effect, or the solver stopped early.")
            total_run = chunk_end

            _run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing stale postProcessing")
            _run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess flow monitor")
            _, vals = read_vol_average_dat(f"{case_dir}/postProcessing/volAverage1/0/volFieldValue.dat")
            cur_avg = vals[-1]

            if prev_avg is not None and prev_avg != 0:
                rel_change = abs(cur_avg - prev_avg) / abs(prev_avg)
                streak = streak + 1 if rel_change <= rel_tol else 0
                log_fn(f"  [{total_run} iterations total] volAverage({check_field}) = {cur_avg:.6g} "
                       f"(change since last chunk: {rel_change * 100:.3f}%, target <={rel_tol * 100:.2g}%"
                       f"{f'; {streak}/{converged_chunks} consecutive' if streak else ''})")
                # ONE small change is not convergence - on an oscillating
                # signal it is a turning point, so this test would otherwise
                # fire preferentially at the extremes of the cycle rather than
                # at a settled state. Demand a run of them.
                if streak >= converged_chunks:
                    converged = True
            else:
                log_fn(f"  [{total_run} iterations total] volAverage({check_field}) = {cur_avg:.6g} (first chunk)")
            prev_avg = cur_avg
            history.append({"iteration": total_run, "value": cur_avg})
            _save_history(case_dir, history)

            # A genuinely turbulent flow (fan jet on a wall) never produces a
            # run of small changes, so the streak test above would burn the
            # whole max_iterations budget before the post-loop check reached
            # the same verdict. Ask the same question here, as soon as there
            # is enough history to answer it.
            if not converged and _is_stable_oscillation(
                    [h["value"] for h in history], oscillation_window, oscillation_growth_tol):
                accepted_oscillation = True

            log_fn(f"  Copying fields from {latest}/ to 0/ (excluding T - that's our fresh UV-decay "
                   f"starting condition, not a flow quantity) so the next chunk continues from here...")
            r = _run_wsl_or_raise(f"ls {latest}/ | grep -v '^uniform$' | grep -v '^T$'",
                                   case_dir_wsl, "listing converged field files")
            field_files = r.stdout.split()
            cp_targets = " ".join(f"{latest}/{f}" for f in field_files)
            _run_wsl_or_raise(f"cp -f {cp_targets} 0/", case_dir_wsl, "copying converged fields")
            _run_wsl_or_raise(
                "for d in [0-9]*/; do [ \"$d\" = \"0/\" ] || rm -rf \"$d\"; done",
                case_dir_wsl, "cleaning time directories",
            )

            if converged or accepted_oscillation:
                break

        if not converged and not accepted_oscillation:
            values = [h["value"] for h in history]
            accepted_oscillation = _is_stable_oscillation(values, oscillation_window, oscillation_growth_tol)
            if not accepted_oscillation:
                diagnostic = _oscillation_diagnostic(
                    history, oscillation_window, oscillation_growth_tol, rel_tol, n_iterations, check_field,
                    converged_chunks=converged_chunks)
                raise FlowConvergenceUndecided(
                    f"Flow field did not converge within {total_run} iterations, and there's no clear "
                    f"verdict on whether it's a stable bounded oscillation either: {diagnostic['summary']}",
                    diagnostic=diagnostic, total_iterations=total_run,
                )
    finally:
        if method == "lts":
            log_fn("Restoring ddtSchemes.default = Euler (the later transient pimpleFoam "
                   "decay run needs real time-accurate stepping, not LTS)...")
            set_lts_ddt_scheme(case_dir, False)

    if converged:
        log_fn(f"Flow field converged after {total_run} iterations total "
               f"(volAverage({check_field}) changed <={rel_tol * 100:.2g}% in each of the last "
               f"{converged_chunks} consecutive chunks).")
    else:
        log_fn(f"Flow field did not fully converge within {total_run} iterations, but "
               f"volAverage({check_field}) has settled into a bounded oscillation (not still "
               f"growing or drifting) rather than genuinely diverging - accepting it as-is. "
               f"This is expected for flows with a jet/fan impinging directly on a wall or "
               f"floor (real unsteady turbulence, not a numerical convergence problem); "
               f"verified empirically that the downstream T-decay/eACH_uv result is "
               f"insensitive to exactly which point in the oscillation the field is frozen at.")

    log_fn("Re-enabling scalarTransport1 for the transient UV-decay run...")
    set_function_object_enabled(case_dir, "scalarTransport1", True)

    log_fn(f"Restoring system/volAverageDict to track T (was tracking {check_field} "
           f"for flow convergence) - the decay-analysis step downstream needs it.")
    write_vol_average_dict(case_dir)
    log_fn("  Clearing postProcessing/ from the p-tracking runs above - otherwise OpenFOAM "
           "detects the changed field name and versions the T output into volFieldValue_0.dat "
           "instead of the plain volFieldValue.dat that decay_analysis reads, silently leaving "
           "the stale p data in place under the expected filename.")
    _run_wsl("rm -rf postProcessing", case_dir_wsl)

    return str(total_run), converged


def continue_flow_convergence(case_dir, additional_iterations, n_iterations=500, fan_entry=None,
                               log_fn=print, should_stop=None, method="simple", rel_tol=0.01,
                               oscillation_window=6, oscillation_growth_tol=1.5, solver_log_fn=None,
                               should_pause=None, converged_chunks=3):
    """Resume a case directory whose flow convergence previously stopped
    without a verdict (a caller caught FlowConvergenceUndecided and the
    user chose "continue") - runs `additional_iterations` more on top of
    whatever's already been done, reusing the existing mesh/fields/
    fvOptions on disk untouched (no mesh regeneration, no potentialFoam
    reset - see converge_flow_field's resume=True docstring).

    May raise FlowConvergenceUndecided again if `additional_iterations`
    still isn't enough to reach a verdict - exactly like a fresh attempt
    would, just picking up from the accumulated history instead of
    starting over. That's expected, not a bug: the caller should present
    the same decision again (continue further, or accept).

    should_pause: forwarded straight to converge_flow_field/
    run_wsl_streaming - suspends the active solver process in place
    instead of killing it, unlike should_stop.
    """
    history = _load_history(case_dir)
    already_run = history[-1]["iteration"] if history else 0
    return converge_flow_field(
        case_dir, n_iterations=n_iterations, fan_entry=fan_entry, log_fn=log_fn,
        max_iterations=already_run + additional_iterations, rel_tol=rel_tol, should_stop=should_stop,
        method=method, oscillation_window=oscillation_window, oscillation_growth_tol=oscillation_growth_tol,
        solver_log_fn=solver_log_fn, resume=True, should_pause=should_pause,
        converged_chunks=converged_chunks,
    )


def case_awaiting_flow_decision(case_dir, oscillation_window=6, oscillation_growth_tol=1.5,
                                 rel_tol=0.01, n_iterations=500, check_field="p",
                                 converged_chunks=3):
    """Whether `case_dir` has flow-convergence chunk history on disk but
    never reached a resolved verdict (converged, accepted, or an explicit
    user override via resume_case_setup) - lets a resume be offered even
    from a FRESH server process/session with no memory of the original
    run pausing (previously the only way to resume was within the exact
    same server process that caught FlowConvergenceUndecided - a real gap,
    since restarting the server, or coming back to a case days later, are
    completely normal things to do).

    Recognized by: chunk history exists (flow convergence was attempted),
    but 0/fluenceRate doesn't (only written by _finish_case_setup, which
    only runs once flow convergence is actually resolved one way or
    another) - i.e. this case got partway through setup_case() and never
    finished. A case that completed normally has both a full history AND
    fluenceRate, so this correctly returns None for it, not a false
    "pending decision".

    Returns {"diagnostic": ..., "total_iterations": ...} (same shape
    FlowConvergenceUndecided carries) if there's something to resume, or
    None if there's no history, or it's already past this stage.
    """
    history = _load_history(case_dir)
    if not history:
        return None
    if Path(f"{case_dir}/0/fluenceRate").exists():
        return None
    diagnostic = _oscillation_diagnostic(
        history, oscillation_window, oscillation_growth_tol, rel_tol, n_iterations, check_field,
        converged_chunks=converged_chunks)
    return {"diagnostic": diagnostic, "total_iterations": history[-1]["iteration"]}


def _flow_rate_dict(patches):
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      flowRateDict;", "}", "",
        "functions", "{",
        "    readPhi", "    {",
        "        type            readFields;",
        '        libs            ("libfieldFunctionObjects.so");',
        "        fields          (phi);",
        "        executeControl  timeStep;",
        "        executeInterval 1;",
        "    }", "",
    ]
    for patch in patches:
        lines += [
            f"    {patch}FlowRate", "    {",
            "        type            surfaceFieldValue;",
            '        libs            ("libfieldFunctionObjects.so");',
            "        fields          (phi);",
            "        operation       sum;",
            "        regionType      patch;",
            f"        name            {patch};",
            "        executeControl  timeStep;",
            "        executeInterval 1;",
            "        writeControl    timeStep;",
            "        writeInterval   1;",
            "        writeFields     false;",
            "    }", "",
        ]
    lines += ["}", ""]
    return "\n".join(lines)


def check_ach_delivery(case_dir, room_volume, ach, outlet_patches=("outlet",), tol=0.10, log_fn=print):
    """Measure the CFD's actual delivered ventilation flow rate (summing the
    solved flux field `phi` over the outlet patch/es) and compare it against
    the nominal ACH target - independent of, and a precondition for trusting,
    anything the later contaminant/UV phases report.

    This exists because a diffuser's velocity-direction model can silently
    under- (or over-) deliver its intended flow rate while looking completely
    normal in every flow-convergence log: a "ceiling" diffuser giving every
    face the same 3D speed but tilting most of it tangentially (Coanda
    spread) delivered only ~38% of its nominal target on a real case, even
    though the flow field itself looked unremarkable - residuals plateaued
    the same way a healthy, correctly-flowing case's would. No amount of
    flow-residual or T-plateau checking downstream would ever catch this;
    only measuring the actual delivered flow rate does. Cheap - reads
    already-solved fields, no new solve needed (confirmed: a few seconds).

    outlet_patches: sum flow across all of them (e.g. ("outlet", "outlet2")
    when a 2nd outlet is enabled) - net ventilation flow is what leaves via
    the outlet(s), regardless of how many inlets fed it.

    Returns a dict: {measured_flow_rate, nominal_flow_rate, measured_ach,
    nominal_ach, ratio, within_tolerance, tol}. ratio = measured/nominal;
    within_tolerance is True iff ratio is within [1-tol, 1+tol].
    """
    case_dir_wsl = _wsl_path(case_dir)
    _write_case_file(case_dir, "system/flowRateDict", _flow_rate_dict(outlet_patches))

    r = _run_wsl_or_raise("postProcess -dict system/flowRateDict -latestTime", case_dir_wsl,
                           "measuring delivered ACH (outlet flow rate)")
    _run_wsl("rm -rf postProcessing", case_dir_wsl)

    measured_flow_rate = 0.0
    for patch in outlet_patches:
        m = re.search(rf"sum\({patch}\) of phi = ([\-0-9.eE+]+)", r.stdout)
        if not m:
            raise RuntimeError(
                f"Could not parse outlet flow rate for patch {patch!r} from postProcess output:\n{r.stdout}")
        measured_flow_rate += abs(float(m.group(1)))

    nominal_flow_rate = ach * room_volume / 3600.0
    ratio = measured_flow_rate / nominal_flow_rate if nominal_flow_rate else float("inf")
    measured_ach = measured_flow_rate * 3600.0 / room_volume if room_volume else 0.0
    within_tolerance = (1 - tol) <= ratio <= (1 + tol)

    if within_tolerance:
        log_fn(f"ACH delivery check: measured {measured_ach:.4g} /hr vs nominal {ach:.4g} /hr "
               f"(ratio {ratio:.2%}) - within +/-{tol:.0%} tolerance, OK.")
    else:
        log_fn(f"ACH delivery check WARNING: measured {measured_ach:.4g} /hr vs nominal {ach:.4g} /hr "
               f"(ratio {ratio:.2%}) - OUTSIDE +/-{tol:.0%} tolerance. The mesh/BCs are not delivering "
               f"the intended ventilation rate - every downstream result (T_ss, eACH_uv, mixing "
               f"efficiency) will be computed against the WRONG effective ACH until this is fixed "
               f"(check inlet/outlet geometry, diffuser type, or opening size).")

    return {
        "measured_flow_rate": measured_flow_rate, "nominal_flow_rate": nominal_flow_rate,
        "measured_ach": measured_ach, "nominal_ach": ach, "ratio": ratio,
        "within_tolerance": within_tolerance, "tol": tol,
    }


def check_settings_grid_alignment(settings, room, cell_size, tol=1e-6):
    """Check whether the inlet/outlet/inlet2/outlet2 openings and the
    contaminant source position will be moved
    once their positions/sizes snap to the mesh grid (see
    mesh_gen.opening_grid_alignment / contaminant_source.
    source_box_grid_alignment) - lets a caller warn a user BEFORE running
    a simulation, rather than discovering the same class of problem only
    after a multi-hour run the way check_ach_delivery does (confirmed on
    a real project: a 0.3x0.3m inlet on a 0.1m mesh silently carved down
    to 0.2x0.2m - 44% of the requested area - and check_ach_delivery only
    caught the SYMPTOM, a delivery-rate shortfall, after the flow field
    had already fully converged).

    settings: a project's settings dict (inlet-wall/inlet-y-input/etc,
    the same shape run_pipeline/scenario_runs already expect).
    inject-x/y/z-input, if present, gets
    checked too.

    Returns a list of dicts, one per opening/zone whose actual carved
    size differs from its nominal (requested) size by more than tol on
    any axis: {"name", "nominal", "actual"} - empty if everything's
    already grid-aligned. Thanks to the outward-only snap, "actual" is
    always >= "nominal" on every axis - a non-empty entry always means
    "will end up slightly LARGER than typed", never a silent shortfall.
    """
    mismatches = []

    def _check_opening(name, prefix, enabled=True):
        if not enabled:
            return
        wall = settings[f"{prefix}-wall"]
        center_frac = center_frac_for_wall(wall, settings[f"{prefix}-y-input"], settings[f"{prefix}-z-input"], room)
        size = (settings[f"{prefix}-size-w"], settings[f"{prefix}-size-h"])
        nominal, actual = opening_grid_alignment(wall, room.x, room.y, room.z, center_frac, size, cell_size)
        if any(abs(a - n) > tol for n, a in zip(nominal, actual)):
            mismatches.append({"name": name, "nominal": nominal, "actual": actual})

    _check_opening("Inlet", "inlet")
    _check_opening("Outlet", "outlet")
    _check_opening("2nd inlet", "inlet2", enabled=bool(settings.get("inlet2-enable")))
    _check_opening("2nd outlet", "outlet2", enabled=bool(settings.get("outlet2-enable")))

    # The source zone's SIZE can no longer mismatch - it is configured in whole
    # mesh cells (contaminant_source.source_size_from_cells), so it is exact by
    # construction. Its CENTRE still can: an off-lattice centre forces the box
    # edges to snap outward, growing the zone by up to a cell per axis. Report
    # that instead, as a position the caller can offer to correct - the same
    # treatment openings get from suggest_opening_center_fix.
    has_source_inputs = all(k in settings for k in ("inject-x-input", "inject-y-input", "inject-z-input"))
    if has_source_inputs:
        center = (settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"])
        size = resolve_source_size(settings, cell_size, (room.x, room.y, room.z))
        cur, sug = suggest_source_center_fix(center, size, cell_size, (room.x, room.y, room.z))
        if any(abs(a - b) > tol for a, b in zip(cur, sug)):
            mismatches.append({"name": "Contaminant source position", "nominal": cur, "actual": sug})

    return mismatches


def walk_opening_alignment_conflicts(settings, room, cell_size, tol=1e-6):
    """Yields ONE grid-alignment conflict at a time, across Inlet/Outlet
    (always) and 2nd inlet/2nd outlet (only when their own -enable flag is
    set) - the contaminant source zone is NOT covered here, it stays on
    check_settings_grid_alignment's existing bulk check (a single
    symmetric cube has no width/height/position-pair/wall-center-bias
    distinction for this per-opening flow to add value to).

    Each yielded item is a dict:
        {"opening": "Inlet"|"Outlet"|"2nd inlet"|"2nd outlet",
         "kind": "size"|"center",
         "field_id": <settings key this fix would update, e.g. "inlet-size-w">,
         "axis_label": "width"|"height"|"position 1"|"position 2",
         "current": <float, meters>, "suggested": <float, meters>}

    Size conflicts for an opening are always yielded before its center
    conflicts (see mesh_gen.suggest_opening_center_fix's own docstring for
    why - a center fix is only meaningful once the size it's computed
    against is an actual exact multiple of cell_size).

    Drive with next()/send(agree: bool) - prime with next(), then
    gen.send(True/False) per decision. agree=True makes `suggested` the
    value in effect for anything computed AFTER it (e.g. a size fix
    feeds into that same axis's own subsequent center check); agree=False
    keeps `current` in effect. Declining a SIZE conflict on an axis
    silently skips that axis's own CENTER conflict (mathematically, a
    non-cell-multiple size can never have both edges land on the grid
    regardless of center - see suggest_opening_center_fix's docstring).
    Ends naturally via StopIteration (the generator protocol) once every
    enabled opening's conflicts are exhausted.
    """
    def _walk_one_opening(name, prefix):
        wall = settings[f"{prefix}-wall"]
        center_frac = center_frac_for_wall(wall, settings[f"{prefix}-y-input"], settings[f"{prefix}-z-input"], room)
        nominal_size = (settings[f"{prefix}-size-w"], settings[f"{prefix}-size-h"])
        suggested_size = suggest_opening_size_fix(wall, room.x, room.y, room.z, nominal_size, cell_size)

        effective_size = list(nominal_size)
        size_axis_ok = [True, True]
        size_field_ids = (f"{prefix}-size-w", f"{prefix}-size-h")
        size_axis_labels = ("width", "height")

        for i in range(2):
            if abs(suggested_size[i] - nominal_size[i]) <= tol:
                continue
            agree = yield {
                "opening": name, "kind": "size", "field_id": size_field_ids[i],
                "axis_label": size_axis_labels[i],
                "current": nominal_size[i], "suggested": suggested_size[i],
            }
            if agree:
                effective_size[i] = suggested_size[i]
            else:
                size_axis_ok[i] = False

        (cur1, cur2), (sug1, sug2) = suggest_opening_center_fix(
            wall, room.x, room.y, room.z, center_frac, tuple(effective_size), cell_size)
        center_field_ids = (f"{prefix}-y-input", f"{prefix}-z-input")
        center_axis_labels = ("position 1", "position 2")
        currents, suggesteds = (cur1, cur2), (sug1, sug2)

        for i in range(2):
            if not size_axis_ok[i] or abs(suggesteds[i] - currents[i]) <= tol:
                continue
            yield {
                "opening": name, "kind": "center", "field_id": center_field_ids[i],
                "axis_label": center_axis_labels[i],
                "current": currents[i], "suggested": suggesteds[i],
            }

    for name, prefix, enabled in [
        ("Inlet", "inlet", True),
        ("Outlet", "outlet", True),
        ("2nd inlet", "inlet2", bool(settings.get("inlet2-enable"))),
        ("2nd outlet", "outlet2", bool(settings.get("outlet2-enable"))),
    ]:
        if enabled:
            yield from _walk_one_opening(name, prefix)


def setup_case(guv_path, case_dir, template_case_dir=None, cell_size=0.1, Z=2.0, nbins=25,
               source_field="T", map_from_case=None, map_from_time=0, ach=3.0,
               inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
               inlet_diffuser_type="direct",
               outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
               inlet2_wall=None, inlet2_center=None, inlet2_size=None,
               inlet2_diffuser_type="direct",
               outlet2_wall=None, outlet2_center=None, outlet2_size=None,
               converge_flow=True, simple_foam_iterations=500, flow_convergence_method="simple",
               flow_rel_tol=0.01, flow_max_iterations=20000, n_procs=None,
               oscillation_window=6, oscillation_growth_tol=1.5, converged_chunks=3,
               ach_delivery_tol=0.10,
               momentum_relaxation=None, scalar_relaxation=None, adaptive_t_relaxation=False,
               scalar_transport_ncorr=None, scalar_transport_tolerance=None,
               pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5, max_co=None,
               fan_speed=None, fan_center=None, fan_direction=(0, 0, -1),
               fan_disk_radius=0.6, fan_disk_thickness=0.2, fan_height=None,
               sealed=False, mechanical_ach_only=False,
               refine_lamp_positions=None, refine_lamp_radius=None, refine_lamp_levels=2,
               refine_opening_depth=None, refine_opening_levels=1,
               log_fn=print, should_stop=None, solver_log_fn=None, should_pause=None,
               solve_semaphore=None):
    """Set up an OpenFOAM case end-to-end from a .guv project. Returns a dict
    summarizing the run (room dims, lamp count, fluence/k ranges, zone count).

    ach: target air changes per hour [1/hr] - inlet velocity is derived from
    this and the room volume/inlet area (see initial_fields.compute_inlet_velocity),
    rather than a fixed velocity that would silently drift ACH as room size changes.

    inlet_diffuser_type/inlet2_diffuser_type: "direct" (a single vector
    straight into the room, matching every version of this pipeline before
    this parameter existed) or "ceiling" (a real diffuser's discharge:
    velocity spread radially outward across the opening, in the plane of
    its wall - see initial_fields.resolve_inlet_velocity/
    compute_radial_inlet_velocities for the physical justification).

    inlet2_wall/center/size, outlet2_wall/center/size: an optional 2nd inlet
    and/or 2nd outlet, each on any of the 6 room walls. When given, both
    inlets share the same velocity magnitude (see
    initial_fields.compute_inlet_velocities) - flow splits between them in
    proportion to each inlet's own opening area. None (the default) means
    "no 2nd opening", matching today's single-inlet/outlet behavior exactly.

    converge_flow: if True, run simpleFoam (see converge_flow_field()) to get
    a genuinely converged flow field for this mesh, rather than trusting
    mapFields' interpolated guess as-is.

    flow_max_iterations: hard cap on total simpleFoam/pimpleFoam iterations
    during flow convergence (see converge_flow_field's own docstring for
    what happens when this is hit without converging) - GUI-exposed as a
    cross-project "advanced" default (Settings menu, right of File), like
    flow_rel_tol/cell_size/nbins above.

    oscillation_window/oscillation_growth_tol: passed straight through to
    converge_flow_field's _is_stable_oscillation check (see its own
    docstring) - the decision that lets a persistently-oscillating flow
    (a jet/fan impinging on a wall or floor, common in these room-
    ventilation cases) get ACCEPTED as "good enough to proceed" rather than
    raising, once bounded and not still growing/drifting. Previously
    hardcoded with no GUI visibility at all - now a cross-project
    "advanced" default like the others.

    ach_delivery_tol: fractional tolerance (0.10 = 10%) for
    check_ach_delivery(), run right after flow convergence - independent of
    whether the flow itself converged or was accepted via oscillation, this
    checks whether the mesh/BCs are actually delivering the intended `ach`
    at all. A flow-residual or T-plateau check downstream would never catch
    a diffuser under/over-delivering its nominal flow rate (see
    check_ach_delivery's docstring for the real case that motivated this).
    GUI-exposed as a cross-project "advanced" default.

    momentum_relaxation/scalar_relaxation: SIMPLE under-relaxation factors
    for U/(k|omega) and T respectively (see splice.set_relaxation_factors)
    - None (the default) leaves the template's own values untouched.
    GUI-exposed as cross-project "advanced" defaults too.

    adaptive_t_relaxation: if True, scalar_relaxation is ignored and T's
    relaxation is instead computed from THIS case's own kUV.max once it's
    known (see splice.compute_adaptive_scalar_relaxation) - applied inside
    _finish_case_setup, after the fluence/kUV field is written, since
    kUV.max isn't known any earlier than that (mesh + flow convergence
    both have to happen first). scalar_relaxation is still used for the
    momentary static value the template starts with before that point
    (flow convergence itself never touches T - scalarTransport1 is
    disabled during it - so what T's relaxation is set to before then has
    no effect on anything).

    scalar_transport_ncorr/scalar_transport_tolerance: the scalarTransport1
    function object's OWN outer-correction count/residual target (see
    splice.set_scalar_transport_correction) - T is solved by this function
    object, entirely outside PIMPLE's own outer-corrector loop, so relaxing
    T only avoids biasing a transient (pimpleFoam) run's result if these are
    set high/tight enough for scalarTransport1's loop to actually converge
    each timestep; OpenFOAM's own defaults (nCorr=0, tolerance=1) mean it
    never does - see "OpenFoam settings background.md" at the repo root.
    None (the default) leaves the template's own values untouched.
    GUI-exposed as cross-project "advanced" defaults too.

    pimple_end_time/pimple_write_interval: the transient UV-decay run's
    simulated duration [s] and write cadence [s] - GUI-exposed per-project
    (Project Setup tab), like Z and ach above.

    pimple_delta_t/flow_rel_tol/cell_size/nbins: GUI-exposed too, but as
    cross-project "advanced" defaults (Settings menu, right of File) rather
    than per-project fields - see app_settings.py.

    fan_speed: if given (m/s, see fan.SPEED_RANGE), adds an optional mixing
    fan (see fan.py) - a cylindrical cellZone (default radius 0.6m, a 1.2m
    diameter fan) with a meanVelocityForce driving that zone's mean velocity
    to fan_speed in fan_direction (default straight down, like a ceiling
    fan). Stays active through flow convergence *and* the pimpleFoam phase
    (a real fan affects the whole scenario, not just part of it) - unlike
    the UV/source entries, which only apply once scalar transport starts.
    fan_center defaults to room center in x/y, 30cm below the ceiling, if
    not given.

    sealed: True to build a sealed room - no ventilation at all (decay mode
    only; ach is expected to be <=0, but this must be requested explicitly
    by the caller, never inferred from ach here, since setup_case is shared
    with steady-state, which has no sensible zero-ACH case). The inlet/
    outlet openings (and inlet2/outlet2, if present) are built as real wall
    patches instead of zero-velocity flow patches (see
    mesh_gen.create_patch_dict), potentialFoam is skipped (see
    converge_flow_field's skip_potential_flow), and the post-convergence
    ACH-delivery check is skipped (there's no ventilation to measure).
    Requires fan_speed - a sealed room with no fan has no driving force at
    all.

    mechanical_ach_only: True to skip the fluence/inactivation-rate/cellZone/
    fvOptions UV pipeline entirely (see _finish_case_setup) and write an
    empty constant/fvOptions instead - for a pure ventilation-only study
    (e.g. no UV lamps yet, or deliberately ignoring UV present in the
    project) where there's nothing physical to bin. Independent of sealed
    and of the project's actual lamp count - this is a user choice, not a
    derived one.

    refine_lamp_positions/refine_lamp_radius/refine_lamp_levels: if
    refine_lamp_positions is given (a list of (x,y,z) lamp coordinates)
    and refine_lamp_radius is not None, locally refine the mesh within
    that radius of every lamp (mesh_gen.lamp_refine_topo_set_dict +
    refineMesh, run refine_lamp_levels times - each level halves the
    local cell size, so 2 levels = 4x finer, not 2x) before the mesh's
    inlet/outlet patches are carved. The near-field UV fluence rate is
    dominated by a near-singular peak right at each lamp that a uniform
    mesh badly under-resolves (confirmed: a 0.08m mesh only captures
    ~1.7% of the true continuum peak there) - this spends extra cells
    only where that actually matters, instead of refining the whole room.
    None (the default) skips this entirely, matching every mesh built
    before this parameter existed.

    refine_opening_depth/refine_opening_levels: same idea for the inlet/
    outlet (and inlet2/outlet2, if enabled) - refines a halo extending
    refine_opening_depth beyond each opening's own footprint (into the
    room, plus padding on the two in-plane edges), so the near-boundary
    flow around each opening is also better resolved. None skips this.

    Both refinements run AFTER blockMesh but BEFORE the existing
    topoSet/createPatch step that carves the inlet/outlet boundary
    patches - so createPatch's own face selection (a box right at the
    wall) picks up the now-finer faces there, giving genuinely finer
    boundary patches, not just finer interior cells nearby.
    """
    if sealed and fan_speed is None:
        raise ValueError("A sealed room (ach<=0) needs a mixing fan (fan_speed) - "
                          "with no ventilation and no fan, there's no way for the "
                          "flow field to develop at all.")

    case_dir_wsl = _wsl_path(case_dir)
    summary = {}

    # Everything a brand-new case directory needs (the directory tree itself,
    # case.foam, and the static template config files) is created via ONE
    # WSL-native command rather than Windows-side pathlib/shutil. Confirmed
    # 2026-08-03 (see wsl_utils.windows_path_to_wsl_mnt's docstring): writes
    # to a *newly created* directory/file made from the Windows side through
    # \\wsl.localhost\... aren't reliably durable to the actual WSL
    # filesystem - a real sweep lost an entire freshly-created case tree
    # this way (blockMesh's `cd` into it failed with "No such file or
    # directory" despite Python having reported the mkdir/copy as
    # successful). Once a directory is genuinely established WSL-side,
    # subsequent Windows<->WSL access into it is fine (that's the pattern
    # the rest of this pipeline already relies on, e.g. write_mesh_dicts
    # below writing into system/ right after this).
    setup_cmd = (f'mkdir -p "{case_dir_wsl}/system" "{case_dir_wsl}/constant" '
                 f'&& touch "{case_dir_wsl}/case.foam"')
    if template_case_dir is not None:
        template_wsl = _windows_path_to_wsl_mnt(str(template_case_dir))
        for name in ("controlDict", "fvSchemes", "fvSolution", "volAverageDict"):
            setup_cmd += (f' ; cp "{template_wsl}/system/{name}" '
                          f'"{case_dir_wsl}/system/{name}" 2>/dev/null')
        for name in ("transportProperties", "turbulenceProperties"):
            setup_cmd += (f' ; cp "{template_wsl}/constant/{name}" '
                          f'"{case_dir_wsl}/constant/{name}" 2>/dev/null')
        setup_cmd += " ; true"
    _run_wsl_or_raise(setup_cmd, "/", "create case directory")
    log_fn("Touched case.foam (ParaView marker file) - present from the start so it's "
           "there regardless of whether this run finishes, fails, or gets interrupted.")

    if template_case_dir is not None:
        log_fn(f"Copied static config from {template_case_dir}")
        if momentum_relaxation is not None or scalar_relaxation is not None:
            set_relaxation_factors(case_dir, momentum_factor=momentum_relaxation,
                                    scalar_factor=scalar_relaxation)
        if scalar_transport_ncorr is not None or scalar_transport_tolerance is not None:
            set_scalar_transport_correction(case_dir, ncorr=scalar_transport_ncorr,
                                             tolerance=scalar_transport_tolerance)
        if sealed:
            log_fn("Sealed room: setting a pressure reference cell (PIMPLE/SIMPLE) - with "
                   "every boundary now a wall, there's no fixedValue p patch left to pin "
                   "down the absolute pressure level...")
            set_pressure_reference_cell(case_dir)

    log_fn(f"Loading project {guv_path} ...")
    project = Project.load(guv_path)
    room = next(iter(project.rooms.values()))
    log_fn(f"  Room {room.x}x{room.y}x{room.z} {room.units}, {len(room.lamps)} lamp(s)")
    summary["room"] = (room.x, room.y, room.z, str(room.units))
    summary["n_lamps"] = len(room.lamps)

    if sealed:
        log_fn("Sealed room (ACH<=0): inlet/outlet openings will be closed off as walls - "
               "mixing relies entirely on the fan...")
    log_fn("Writing mesh dicts (blockMeshDict/topoSetDict/createPatchDict)...")
    write_mesh_dicts(case_dir, room.x, room.y, room.z, cell_size=cell_size,
                      inlet_wall=inlet_wall, inlet_center=inlet_center, inlet_size=inlet_size,
                      outlet_wall=outlet_wall, outlet_center=outlet_center, outlet_size=outlet_size,
                      inlet2_wall=inlet2_wall, inlet2_center=inlet2_center, inlet2_size=inlet2_size,
                      outlet2_wall=outlet2_wall, outlet2_center=outlet2_center, outlet2_size=outlet2_size,
                      sealed=sealed)

    log_fn("Running blockMesh...")
    _run_wsl_or_raise("blockMesh", case_dir_wsl, "blockMesh")

    if refine_lamp_positions and refine_lamp_radius is not None:
        log_fn(f"Locally refining mesh within {refine_lamp_radius}m of each of "
               f"{len(refine_lamp_positions)} lamp(s), {refine_lamp_levels} level(s) "
               f"({2 ** refine_lamp_levels}x finer there)...")
        write_lamp_refine_topo_set_dict(case_dir, refine_lamp_positions, refine_lamp_radius)
        for level in range(refine_lamp_levels):
            _run_wsl_or_raise("topoSet -dict system/lampRefineTopoSetDict",
                               case_dir_wsl, f"topoSet (lamp refine, level {level + 1})")
            write_refine_mesh_dict(case_dir, "lampRefineCells")
            _run_wsl_or_raise("refineMesh -dict system/refineMeshDict -overwrite",
                               case_dir_wsl, f"refineMesh (lamp refine, level {level + 1})")

    if refine_opening_depth is not None:
        boxes = [_opening_refine_box(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size,
                                      refine_opening_depth, cell_size=cell_size),
                 _opening_refine_box(outlet_wall, room.x, room.y, room.z, outlet_center, outlet_size,
                                      refine_opening_depth, cell_size=cell_size)]
        if inlet2_wall is not None:
            boxes.append(_opening_refine_box(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size,
                                              refine_opening_depth, cell_size=cell_size))
        if outlet2_wall is not None:
            boxes.append(_opening_refine_box(outlet2_wall, room.x, room.y, room.z, outlet2_center, outlet2_size,
                                              refine_opening_depth, cell_size=cell_size))
        log_fn(f"Locally refining mesh within {refine_opening_depth}m of {len(boxes)} opening(s), "
               f"{refine_opening_levels} level(s) ({2 ** refine_opening_levels}x finer there)...")
        write_opening_refine_topo_set_dict(case_dir, boxes)
        for level in range(refine_opening_levels):
            _run_wsl_or_raise("topoSet -dict system/openingRefineTopoSetDict",
                               case_dir_wsl, f"topoSet (opening refine, level {level + 1})")
            write_refine_mesh_dict(case_dir, "openingRefineCells")
            _run_wsl_or_raise("refineMesh -dict system/refineMeshDict -overwrite",
                               case_dir_wsl, f"refineMesh (opening refine, level {level + 1})")

    log_fn("Running topoSet...")
    _run_wsl_or_raise("topoSet", case_dir_wsl, "topoSet")

    log_fn("Running createPatch -overwrite...")
    _run_wsl_or_raise("createPatch -overwrite", case_dir_wsl, "createPatch")

    log_fn("Running checkMesh...")
    r = _run_wsl_or_raise("checkMesh", case_dir_wsl, "checkMesh")
    if "Mesh OK" not in r.stdout:
        raise RuntimeError(f"checkMesh did not report Mesh OK:\n{r.stdout}")
    log_fn("  Mesh OK")

    fan_entry = None
    if fan_speed is not None:
        center = fan_center or (room.x / 2, room.y / 2, (fan_height if fan_height is not None else room.z - 0.3))
        p1 = (center[0], center[1], center[2] - fan_disk_thickness / 2)
        p2 = (center[0], center[1], center[2] + fan_disk_thickness / 2)
        log_fn(f"Carving fan cellZone at {center}, radius={fan_disk_radius}, speed={fan_speed} m/s...")
        write_fan_topo_set_dict(case_dir, p1, p2, fan_disk_radius)
        _run_wsl_or_raise("topoSet -dict system/fanTopoSetDict", case_dir_wsl, "topoSet (fan zone)")
        fan_entry = fan_fvoptions_entry(fan_speed, direction=fan_direction)
        summary["fan"] = {"center": center, "speed": fan_speed, "direction": fan_direction}

    room_volume = room.x * room.y * room.z
    has_outlet2 = outlet2_wall is not None
    if sealed:
        # Inlet/outlet are now wall patches (see write_mesh_dicts above) -
        # no velocity to compute or resolve. inlet_velocity/inlet2_velocity
        # are still passed as non-None placeholders below (their VALUE is
        # ignored by boundary_field_block(sealed=True), but inlet2_velocity
        # being None vs. not is what controls whether an inlet2 block gets
        # emitted at all - see its docstring).
        log_fn(f"Writing initial fields (0/{{U,p,k,omega,nut,T}}), sealed room "
               f"(room volume={room_volume:.3g} m^3)...")
        inlet_velocity = (0.0, 0.0, 0.0)
        inlet2_velocity = (0.0, 0.0, 0.0) if inlet2_wall is not None else None
    else:
        openings = [opening_actual_area(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size,
                                         cell_size=cell_size)]
        if inlet2_wall is not None:
            openings.append(opening_actual_area(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size,
                                                  cell_size=cell_size))
        total_area = sum(openings)
        v_mag = compute_inlet_velocity(ach, room_volume, total_area)

        # Mesh already exists at this point (blockMesh/topoSet/createPatch
        # above) - resolve_inlet_velocity() can read the "ceiling" diffuser's
        # real per-face geometry straight from constant/polyMesh, no need to
        # wait for the writeCellCentres step further below. Computed once,
        # reused for every write_initial_fields()/restore_boundary_conditions()
        # call in this function - stateless/cheap, and mesh geometry doesn't
        # change mid-run.
        inlet_velocity = resolve_inlet_velocity(
            case_dir, "inlet", inlet_wall,
            opening_center(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size, cell_size=cell_size),
            v_mag, diffuser_type=inlet_diffuser_type,
            half_extents=opening_half_extents(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size,
                                               cell_size=cell_size))
        inlet2_velocity = None
        if inlet2_wall is not None:
            inlet2_velocity = resolve_inlet_velocity(
                case_dir, "inlet2", inlet2_wall,
                opening_center(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size, cell_size=cell_size),
                v_mag, diffuser_type=inlet2_diffuser_type,
                half_extents=opening_half_extents(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size,
                                                   cell_size=cell_size))
        log_fn(f"Writing initial fields (0/{{U,p,k,omega,nut,T}}), ACH={ach} -> "
               f"inlet velocity magnitude {v_mag:.4g} m/s ({inlet_diffuser_type})"
               + (f", inlet2 ({inlet2_diffuser_type})" if inlet2_velocity else "")
               + f" (room volume={room_volume:.3g} m^3, total inlet area="
               f"{total_area:.3g} m^2)...")
    _ensure_case_subdir(case_dir, "0")
    write_initial_fields(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                          has_outlet2=has_outlet2, sealed=sealed)
    summary["ach"] = ach
    summary["sealed"] = sealed
    if not sealed:
        summary["inlet_velocity"] = inlet_velocity
        if inlet2_velocity:
            summary["inlet2_velocity"] = inlet2_velocity

    log_fn("Running writeCellCentres...")
    _run_wsl_or_raise("postProcess -func writeCellCentres -time 0", case_dir_wsl, "writeCellCentres")
    log_fn("Running writeCellVolumes (needed for a true volume-weighted room-average of "
           "fluence rate/eACH, not just a plain per-cell mean - matters once the mesh isn't "
           "uniform, e.g. local refinement near lamps/openings)...")
    _run_wsl_or_raise("postProcess -func writeCellVolumes -time 0", case_dir_wsl, "writeCellVolumes")

    if map_from_case is not None:
        log_fn("Writing mapFieldsDict...")
        patch_names = read_boundary_patch_names(case_dir)
        write_map_fields_dict(case_dir, patch_names)
        log_fn(f"Running mapFields from {map_from_case} ...")
        map_from_wsl = _wsl_path(map_from_case)
        _run_wsl_or_raise(f"mapFields {map_from_wsl} -sourceTime {map_from_time}", case_dir_wsl, "mapFields")
        log_fn("  mapFields done; regenerating true cell centers/volumes (mapFields overwrites "
               "Cx/Cy/Cz/V too)...")
        _run_wsl_or_raise("postProcess -func writeCellCentres -time 0", case_dir_wsl, "writeCellCentres (post-map)")
        _run_wsl_or_raise("postProcess -func writeCellVolumes -time 0", case_dir_wsl, "writeCellVolumes (post-map)")
        log_fn("  restoring our own boundary conditions (mapFields also clobbers fixedValue "
               "patches like inlet with interpolated garbage)...")
        restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                                     has_outlet2=has_outlet2, sealed=sealed)

    if converge_flow:
        log_fn(f"Converging flow field ({flow_convergence_method}, chunk size="
               f"{simple_foam_iterations} iterations)...")
        _, flow_converged = converge_flow_field(
            case_dir, n_iterations=simple_foam_iterations, fan_entry=fan_entry,
            log_fn=log_fn, should_stop=should_stop, method=flow_convergence_method,
            rel_tol=flow_rel_tol, max_iterations=flow_max_iterations,
            oscillation_window=oscillation_window, oscillation_growth_tol=oscillation_growth_tol,
            converged_chunks=converged_chunks,
            solver_log_fn=solver_log_fn, should_pause=should_pause, skip_potential_flow=sealed,
            n_procs=n_procs, solve_semaphore=solve_semaphore)
        summary["flow_converged"] = flow_converged
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped after flow convergence.")
        log_fn("  restoring our own boundary conditions again (simpleFoam's mesh-derived "
               "boundary values aren't necessarily our fixedValue settings either)...")
        restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                                     has_outlet2=has_outlet2, sealed=sealed)

        if sealed:
            log_fn("Skipping ACH-delivery check - sealed room, no ventilation to measure.")
            summary["ach_delivery"] = None
        else:
            outlet_patches = ("outlet", "outlet2") if has_outlet2 else ("outlet",)
            summary["ach_delivery"] = check_ach_delivery(
                case_dir, room_volume, ach, outlet_patches=outlet_patches, tol=ach_delivery_tol, log_fn=log_fn)

    return _finish_case_setup(case_dir, room, Z, nbins, source_field, fan_entry,
                               pimple_end_time, pimple_write_interval, pimple_delta_t, log_fn, summary,
                               max_co=max_co, mechanical_ach_only=mechanical_ach_only,
                               adaptive_t_relaxation=adaptive_t_relaxation)


def _volume_weighted_mean(values, volumes):
    """sum(value*volume)/sum(volume) - the true room average of a per-cell
    field. Plain values.mean() only equals this on a UNIFORM mesh (every
    cell the same volume) - once cell sizes vary (e.g. local refinement
    near a lamp/opening), an unweighted mean silently overweights
    whatever region got refined, since it then has proportionally more
    (smaller) cells sampled there than its true share of the room's
    volume. Confirmed as a real, ~4x error on a real locally-refined
    case (naive mean 11.6 uW/cm^2 vs. the true volume-weighted ~2.9).
    """
    values = np.asarray(values, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    return float(np.sum(values * volumes) / np.sum(volumes))


def _finish_case_setup(case_dir, room, Z, nbins, source_field, fan_entry,
                        pimple_end_time, pimple_write_interval, pimple_delta_t, log_fn, summary,
                        max_co=None, mechanical_ach_only=False, adaptive_t_relaxation=False):
    """Everything setup_case() does after flow convergence is resolved
    (converged, accepted, or explicitly overridden by the user) - factored
    out so resume_case_setup() can reach the exact same steps after
    resolving a previously-undecided flow convergence, without repeating
    (or having to keep in sync by hand) mesh generation, initial-field
    writing, or the flow-convergence call itself.

    mechanical_ach_only: skip the fluence/inactivation-rate/cellZone/
    fvOptions binning below entirely (there's nothing physical to bin - see
    setup_case's own docstring) and write an empty constant/fvOptions
    instead, same as ventilation_control.prepare_ventilation_only_control
    does for its (normally secondary) UV-off control clone - here it's the
    ONLY thing that runs, not a paired control.

    adaptive_t_relaxation: see setup_case's own docstring - applied below,
    right after kUV.max is known, a no-op under mechanical_ach_only (no
    kUV field exists there, nothing to size relaxation against).
    """
    case_dir_wsl = _wsl_path(case_dir)

    if mechanical_ach_only:
        log_fn("Mechanical ACH only: skipping fluence/inactivation-rate/cellZone binning - "
               "writing an empty constant/fvOptions (no UV source)...")
        summary["fluence_range"] = None
        summary["fluence_mean"] = None
        summary["k_range"] = None
        summary["eACH_uv_well_mixed_mean"] = 0.0
        summary["eACH_uv_well_mixed_range"] = None
        summary["n_zones"] = 0
        write_fvoptions_file(case_dir, [])
        set_function_object_enabled(case_dir, "scalarTransport1", True)
        if fan_entry is not None:
            log_fn("  Appending fan entry to fvOptions (stays active for the pimpleFoam phase too)...")
            existing_fvoptions = _read_case_file(case_dir, "constant/fvOptions")
            _write_case_file(case_dir, "constant/fvOptions", existing_fvoptions + fan_entry)
    else:
        ach = summary.get("ach")

        log_fn("Computing fluence rate at cell centers...")
        points = read_cell_centers(case_dir, "0")
        try:
            volumes = read_cell_volumes(case_dir, "0")
        except (RuntimeError, OSError):
            # A case built before writeCellVolumes existed here has no 0/V -
            # every such case is a uniform mesh anyway (local refinement is
            # newer than this), so equal weights give the exact same
            # (correct) answer as a real volume-weighted mean would.
            volumes = np.ones(len(points))
        values = compute_fluence_at_points(room, points)
        fluence_mean = _volume_weighted_mean(values, volumes)
        log_fn(f"  {len(points)} cells, fluence rate range [{values.min():.4g}, {values.max():.4g}], "
               f"mean {fluence_mean:.4g} (volume-weighted)")
        summary["n_cells"] = len(points)
        summary["fluence_range"] = (float(values.min()), float(values.max()))
        summary["fluence_mean"] = fluence_mean
        patch_names = read_boundary_patch_names(case_dir)
        write_scalar_field(case_dir, "fluenceRate", values, patch_names)

        log_fn(f"Computing inactivation rate (Z={Z})...")
        k_values = compute_inactivation_rate(values, Z)
        summary["k_range"] = (float(k_values.min()), float(k_values.max()))
        write_scalar_field(case_dir, "kUV", k_values, patch_names)

        if adaptive_t_relaxation:
            kuv_max = summary["k_range"][1]
            adaptive_relax = compute_adaptive_scalar_relaxation(kuv_max)
            log_fn(f"Adaptive T-relaxation: kUV.max={kuv_max:.4g} -> scalar-relaxation="
                   f"{adaptive_relax:.3g} (overriding whatever static value the template started with; "
                   "see splice.compute_adaptive_scalar_relaxation for the calibration this is based on)...")
            set_relaxation_factors(case_dir, scalar_factor=adaptive_relax)
            summary["adaptive_scalar_relaxation"] = adaptive_relax

        eACH_values = compute_well_mixed_eACH(k_values)
        summary["eACH_uv_well_mixed_mean"] = _volume_weighted_mean(eACH_values, volumes)
        summary["eACH_uv_well_mixed_range"] = (float(eACH_values.min()), float(eACH_values.max()))
        log_fn(f"  eACH_UV well-mixed (volume-averaged) = {summary['eACH_uv_well_mixed_mean']:.4g} /hr "
               f"(vs. ventilation ach={ach} /hr)")

        log_fn(f"Binning into {nbins} cellZones...")
        bin_idx, bin_repr = bin_decay_rates(k_values, nbins)
        zone_names, _ = write_cellzones(case_dir, bin_idx, nbins)
        write_fvoptions(case_dir, zone_names, bin_repr, field_name=source_field)
        summary["n_zones"] = int(sum(1 for b in range(len(zone_names)) if (bin_idx == b).any() and b > 0))

        if fan_entry is not None:
            log_fn("  Re-carving fan cellZone (write_cellzones() above overwrote constant/polyMesh/cellZones "
                   "from scratch, wiping it - topoSet's own merge behavior restores it deterministically, "
                   "same cylinder selection since the mesh hasn't changed)...")
            _run_wsl_or_raise("topoSet -dict system/fanTopoSetDict", case_dir_wsl, "topoSet (restore fan zone)")
            log_fn("  Appending fan entry to fvOptions (stays active for the pimpleFoam phase too)...")
            existing_fvoptions = _read_case_file(case_dir, "constant/fvOptions")
            _write_case_file(case_dir, "constant/fvOptions", existing_fvoptions + fan_entry)

    log_fn("Splicing fvOptions into controlDict...")
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    if n_open != n_close:
        raise RuntimeError(f"Brace mismatch after splice: open={n_open} close={n_close}")
    log_fn(f"  Brace check OK ({{={n_open}, }}={n_close})")

    log_fn(f"Setting pimpleFoam transient run parameters: endTime={pimple_end_time}s, "
           f"writeInterval={pimple_write_interval}s, deltaT={pimple_delta_t}s...")
    set_control_dict_time(case_dir, end_time=pimple_end_time,
                           write_interval=pimple_write_interval, delta_t=pimple_delta_t, max_co=max_co)
    summary["pimple_end_time"] = pimple_end_time
    summary["pimple_write_interval"] = pimple_write_interval

    log_fn("Case setup complete.")
    return summary


def resume_case_setup(case_dir, guv_path, decision, ach, Z, nbins=25, source_field="T",
                       inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
                       inlet_diffuser_type="direct",
                       inlet2_wall=None, inlet2_center=None, inlet2_size=None,
                       inlet2_diffuser_type="direct", outlet2_wall=None,
                       cell_size=0.1, additional_iterations=None,
                       simple_foam_iterations=500, flow_convergence_method="simple",
                       flow_rel_tol=0.01, oscillation_window=6, oscillation_growth_tol=1.5,
                       converged_chunks=3,
                       ach_delivery_tol=0.10,
                       pimple_end_time=120, pimple_write_interval=10, pimple_delta_t=0.5, max_co=None,
                       fan_speed=None, fan_direction=(0, 0, -1), mechanical_ach_only=False,
                       adaptive_t_relaxation=False,
                       log_fn=print, should_stop=None, solver_log_fn=None, should_pause=None):
    """Resume a case directory whose flow convergence previously raised
    FlowConvergenceUndecided - reuses the existing mesh, 0/ fields, and
    constant/fvOptions on disk exactly as setup_case() left them; does NOT
    redo mesh generation, initial-field writing, or mapFields.

    decision: "continue" (run `additional_iterations` more via
    continue_flow_convergence() - may raise FlowConvergenceUndecided again
    if still undecided, exactly like a fresh attempt would, in which case
    the caller should present the same decision again) or "accept" (treat
    the current 0/ state as good enough - an explicit, informed user
    override of the automatic acceptance heuristic, logged as such rather
    than silently equated with it).

    Every parameter here is one already recorded in run_settings.json
    (_MESH_AFFECTING_FIELDS) or a cross-project "advanced" default - the
    caller (app.py) is expected to have already validated the current GUI
    settings against run_settings.json (see app._settings_mismatch, the
    same check the existing "Continue to longer duration" feature uses)
    before calling this, so a resume can never silently apply different
    mesh/BC values than what's actually built on disk.
    """
    if decision not in ("continue", "accept"):
        raise ValueError(f"Unknown decision {decision!r} (expected 'continue' or 'accept')")

    project = Project.load(guv_path)
    room = next(iter(project.rooms.values()))
    summary = {"room": (room.x, room.y, room.z, str(room.units)), "n_lamps": len(room.lamps), "ach": ach}

    fan_entry = None
    if fan_speed is not None:
        fan_entry = fan_fvoptions_entry(fan_speed, direction=fan_direction)
        summary["fan"] = {"speed": fan_speed, "direction": fan_direction}

    room_volume = room.x * room.y * room.z
    openings = [opening_actual_area(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size,
                                     cell_size=cell_size)]
    if inlet2_wall is not None:
        openings.append(opening_actual_area(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size,
                                              cell_size=cell_size))
    v_mag = compute_inlet_velocity(ach, room_volume, sum(openings))
    inlet_velocity = resolve_inlet_velocity(
        case_dir, "inlet", inlet_wall,
        opening_center(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size, cell_size=cell_size),
        v_mag, diffuser_type=inlet_diffuser_type,
        half_extents=opening_half_extents(inlet_wall, room.x, room.y, room.z, inlet_center, inlet_size,
                                           cell_size=cell_size))
    inlet2_velocity = None
    if inlet2_wall is not None:
        inlet2_velocity = resolve_inlet_velocity(
            case_dir, "inlet2", inlet2_wall,
            opening_center(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size, cell_size=cell_size),
            v_mag, diffuser_type=inlet2_diffuser_type,
            half_extents=opening_half_extents(inlet2_wall, room.x, room.y, room.z, inlet2_center, inlet2_size,
                                               cell_size=cell_size))
    summary["inlet_velocity"] = inlet_velocity
    if inlet2_velocity:
        summary["inlet2_velocity"] = inlet2_velocity
    has_outlet2 = outlet2_wall is not None

    if decision == "continue":
        if additional_iterations is None:
            raise ValueError("additional_iterations is required when decision='continue'")
        log_fn(f"Resuming flow convergence for {additional_iterations} more iterations...")
        _, flow_converged = continue_flow_convergence(
            case_dir, additional_iterations, n_iterations=simple_foam_iterations, fan_entry=fan_entry,
            log_fn=log_fn, should_stop=should_stop, method=flow_convergence_method, rel_tol=flow_rel_tol,
            oscillation_window=oscillation_window, oscillation_growth_tol=oscillation_growth_tol,
            converged_chunks=converged_chunks,
            solver_log_fn=solver_log_fn, should_pause=should_pause)
        summary["flow_converged"] = flow_converged
    else:
        log_fn("Accepting current flow field state as-is (explicit user override, not the automatic "
               "bounded-oscillation heuristic) - proceeding without further flow iteration.")
        summary["flow_converged"] = False
        summary["flow_accepted_by_user"] = True
        # converge_flow_field() only re-enables scalarTransport1 and restores
        # volAverageDict to track T when it reaches a verdict on its own -
        # since "accept" skips calling it again, that cleanup needs doing
        # here instead, or the later scalarTransport-based fluence/UV phases
        # would silently run with T-transport still disabled.
        set_function_object_enabled(case_dir, "scalarTransport1", True)
        write_vol_average_dict(case_dir)
        _run_wsl("rm -rf postProcessing", _wsl_path(case_dir))

    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped after resuming flow convergence.")

    log_fn("  restoring our own boundary conditions again (simpleFoam's mesh-derived "
           "boundary values aren't necessarily our fixedValue settings either)...")
    restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity,
                                 has_outlet2=has_outlet2)

    outlet_patches = ("outlet", "outlet2") if has_outlet2 else ("outlet",)
    summary["ach_delivery"] = check_ach_delivery(
        case_dir, room_volume, ach, outlet_patches=outlet_patches, tol=ach_delivery_tol, log_fn=log_fn)

    return _finish_case_setup(case_dir, room, Z, nbins, source_field, fan_entry,
                               pimple_end_time, pimple_write_interval, pimple_delta_t, log_fn, summary,
                               max_co=max_co, mechanical_ach_only=mechanical_ach_only,
                               adaptive_t_relaxation=adaptive_t_relaxation)
