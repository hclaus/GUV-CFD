"""Batch "Scenario Runs" orchestration: sweep a steady-state project over
multiple UV susceptibility (Z) and ventilation (ACH) values, one subfolder
per combination.

Key optimization: ACH changes the mesh's inlet velocity (the flow field
must reconverge), but Z only affects the UV dose calculation *after* flow
convergence (fluenceRate is purely geometric; kUV = f(fluenceRate, Z), and
turning kUV into cellZones/fvOptions is pure Python/file-IO, no OpenFOAM
subprocess call). So the flow field is converged once per distinct ACH
value (_build_flow_base) and reused (via a plain directory copy) for
every Z at that ACH (_apply_z) - only the ACH-major outer loop pays for a
full mesh + flow convergence.

Same reasoning applies one level deeper for steady-state sweeps: Phase 1
("source only, no UV") depends only on ach/target_T_ss/source geometry,
never Z, so it's ALSO converged once per ACH (_run_shared_phase1) and
reused via run_steady_state_scenario's own checkpoint-resume mechanism -
every Z sharing that ACH clones the phase1-converged directory and jumps
straight to Phase 2. Decay mode has the equivalent optimization for its
Z-independent UV-off control run (_run_shared_control).

This module only orchestrates repeated calls into run_pipeline.setup_case()
and steady_state_pipeline.run_steady_state_scenario() - it doesn't
duplicate their logic, and deliberately doesn't import from app.py (kept a
plain pipeline-level module, importable/testable without the Dash app) -
the handful of small settings-dict-to-kwargs helpers app.py also has
(_fan_kwargs, _opening_center_frac, etc.) are duplicated locally rather
than imported, for the same reason.
"""
import contextlib
import csv
import io
import json
import math
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait as futures_wait
from pathlib import Path

import numpy as np

from .app_settings import capture_openfoam_settings
from .case_io import (
    read_boundary_patch_names, read_cell_centers, read_cell_volumes, read_latest_time_field,
    read_openfoam_scalar_field, write_scalar_field, snapshot_openfoam_settings,
)
from .cellzones import bin_decay_rates, write_cellzones
from .contaminant_source import (breathing_inlet_direction, breathing_inlet_velocity,
                                  breathing_inlet_velocity_constraint, resolve_source_size,
                                  write_fvoptions_file,
                                  write_source_topo_set_dict)
from .decay_analysis import write_results_summary, mechanical_mixing_efficiency_pct, spatial_coefficient_of_variation
from .fan import fan_fvoptions_entry, write_fan_topo_set_dict
from .fluence import compute_fluence_at_points, compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import compute_inlet_velocities, resolve_case_inlet_velocities
from .mesh_gen import opening_actual_area
from .monitoring import splice_live_vol_average_if_needed
from .monitoring_points import compute_monitoring_results, point_reduction_basis
from .project_status import (
    compute_flow_fingerprint, compute_guv_design_suffix, compute_sim_type_suffix, compute_uv_fingerprint,
    find_reusable_ach_base, get_ach_base_record, load_project_status, now_iso, update_ach_base_status,
    update_combo_status,
)
from .report import combo_summary_metrics
from .run_pipeline import check_ach_delivery, setup_case, _volume_weighted_mean
from .splice import (
    set_control_dict_start_from, set_control_dict_time, splice_fv_options_into_control_dict,
    set_relaxation_factors, compute_adaptive_scalar_relaxation,
)
from .steady_state_pipeline import (
    run_steady_state_scenario, _uv_fvoptions_entries, resolve_phase_delta_ts, merge_project_deltat_settings,
    REFERENCE_TARGET_T_SS, _read_phase1_checkpoint, _read_phase1_pending,
)
from .tclamp_decay import ensure_tclamp_decay_compiled, splice_tclamp_decay_if_needed
from .ventilation_control import prepare_ventilation_only_control, finish_ventilation_only_control
from .visualization import center_frac_for_wall
from .wsl_utils import (
    StoppedByUser, run_wsl_or_raise, run_wsl_streaming, wsl_path, write_case_file,
    read_case_file as _read_case_file,
)

_TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "templates" / "case_template")

# Single, shared cap on how many OpenFOAM processes run at once, regardless
# of which stage (flow convergence, Phase 1, control, Phase 2/decay) they
# belong to (2026-08-11) - replaces the old two-pool _MAX_CONCURRENT_ACH/
# _MAX_CONCURRENT_Z split (up to 3 ACH-level builds + up to 6 Z-level
# solves, bounded separately), which left real spare capacity idle: while
# up to 3 ACH groups were still converging their flow field, the ENTIRE
# Z-pool sat unused even on a machine with cores to spare, since Phase 1/
# control work for an ACH that had just finished converging had nowhere
# to run except that same ACH-pool slot, one stage at a time.
#
# run_sweep/run_decay_sweep now submit every stage (per-ACH flow, Phase 1/
# control, per-Z Phase 2/decay) to ONE shared ORCHESTRATION pool, and get
# priority between stages for free from submission order rather than a
# custom priority queue: every ACH's flow-convergence task is submitted
# before ANY of that ACH's downstream tasks even exist (they're only
# created once their own prerequisite's future resolves), so a
# still-queued flow task for one ACH is always picked up before a
# same-moment-ready Phase 1/control/Phase 2/decay task for another -
# see each function's own ach_worker for the exact per-ACH ordering
# (Phase 1 and control are genuine siblings - both only need the
# converged flow base, not each other - so they're submitted together;
# decay mode has no Phase 1 at all, so control and every Z's own decay
# solve are submitted together instead, immediately once flow finishes).
#
# This value itself no longer sizes that orchestration pool (2026-08-20) -
# it sizes a SEPARATE solve_semaphore instead, acquired only around each
# actual OpenFOAM invocation (see run_pipeline.converge_flow_field's
# solve_semaphore parameter). Confirmed as a real bug when this constant
# doubled as the pool's own worker count: a Z-combo whose own solve
# finished faster than its ACH's shared control run sat blocked holding a
# pool worker for the rest of control's runtime (waiting on
# control_results_future.result(), pure Python, no CPU/subprocess at
# all) - invisible to any process list, but it silently starved OTHER ACH
# groups' work of a pool slot that was never actually busy. The
# orchestration pool is now sized generously (see run_sweep/
# run_decay_sweep's own pool= line) so a thread merely blocked waiting on
# a future never competes with real solve capacity again; this constant
# now purely answers "how many REAL concurrent OpenFOAM processes."
#
# Fallback only when a caller's own adv dict has no "max-concurrent-solves"
# key (e.g. an older saved advanced_settings.json, or a direct/test call
# that doesn't go through app_settings.load_advanced_settings) - the real,
# tunable default lives in app_settings.ADVANCED_SETTINGS_DEFAULTS, see
# its own comment for why this was lowered from 9 to 5 (a real overnight
# sweep failure, 2026-08-20) and why it's memory/reliability, not just
# CPU-core headroom, that should drive this number.
_MAX_CONCURRENT_SOLVES = 5

_UNSAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Matches pimpleFoam's per-timestep "Time = N" banner, not the residual/
# Courant-number/continuity-error lines that follow it - see
# _throttled_solver_callback, which throttles concurrent decay runs' log_fn
# output to this instead of flooding with the full per-iteration dump.
_TIME_LINE_RE = re.compile(r"^Time\s*=\s*[\d.]+\s*$")


def _sanitize(name):
    name = _UNSAFE_FOLDER_CHARS_RE.sub("_", name).strip("_")
    return name or "case"


def _fmt(value):
    """Compact number formatting for folder/file names - 6.0 -> "6", 3.5 -> "3.5"."""
    return f"{value:g}"


def _ach_label(ach):
    """Folder-name-safe label for an ACH value - "sealed" for ACH<=0 (no
    ventilation, fan-only mixing - see run_pipeline.setup_case's `sealed`)
    instead of "0", so a sealed-room subfolder is never confusable at a
    glance with a genuinely low but nonzero ACH one.
    """
    return "sealed" if ach <= 0 else _fmt(ach)


def _subdir_name(z, ach, combo_suffix=""):
    """combo_suffix (see project_status.compute_guv_design_suffix/
    compute_sim_type_suffix) is "" for a project's original design/mode
    (today's exact naming, unchanged) and "_<guv-filename-stem>"/
    "_<sim_type>" (concatenated if both differ) for a genuinely different
    design and/or a switched simulation mode - see those functions' own
    docstrings for the incident this closes: without it, a different
    .guv/mode applied to Z/ACH values a project already used would land
    on those SAME combos and get silently skipped as "already done"
    instead of computing anything new.
    """
    return _sanitize(f"Z{_fmt(z)}_ACH{_ach_label(ach)}{combo_suffix}")


def sweep_combinations(z_values, ach_values):
    """Full cross-product of z_values x ach_values, deduped and sorted,
    ACH-major (outer ACH, inner Z) - matches run_sweep's grouping, so the
    flow-field-reuse optimization above always sees every Z for one ACH
    consecutively.
    """
    zs = sorted(set(z_values))
    achs = sorted(set(ach_values))
    return [(z, ach) for ach in achs for z in zs]


# --- settings-dict -> pipeline-kwargs helpers (duplicated from app.py -
# see module docstring for why) ---

def _fan_kwargs(settings):
    if not settings.get("fan-enable"):
        return {}
    direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
    return dict(
        fan_speed=settings["fan-speed"],
        fan_center=(settings["fan-x-input"], settings["fan-y-input"], settings["fan-z-input"]),
        fan_direction=direction,
        fan_disk_radius=settings["fan-radius"],
        fan_disk_thickness=settings["fan-thickness"],
    )


def _opening_center_frac(settings, prefix, room):
    return center_frac_for_wall(settings[f"{prefix}-wall"], settings[f"{prefix}-y-input"],
                                 settings[f"{prefix}-z-input"], room)


def _second_opening_kwargs(settings, prefix, room):
    if not settings.get(f"{prefix}-enable"):
        return {}
    kwargs = {
        f"{prefix}_wall": settings[f"{prefix}-wall"],
        f"{prefix}_center": _opening_center_frac(settings, prefix, room),
        f"{prefix}_size": (settings[f"{prefix}-size-w"], settings[f"{prefix}-size-h"]),
    }
    if prefix == "inlet2":  # only inlets have a diffuser type, not outlets
        kwargs["inlet2_diffuser_type"] = settings.get("inlet2-diffuser-type", "direct")
    return kwargs


def _gather_monitoring_points(settings):
    if not settings.get("monitoring-enable"):
        return []
    points = []
    for i in (1, 2, 3):
        if not settings.get(f"monitor{i}-enable"):
            continue
        points.append({
            "name": settings.get(f"monitor{i}-name") or f"Point {i}",
            "x": settings[f"monitor{i}-x-input"],
            "y": settings[f"monitor{i}-y-input"],
            "z": settings[f"monitor{i}-z-input"],
            "cells_per_side": settings[f"monitor{i}-cells"],
        })
    return points


def _settling_iterations(lambda_per_hr, target_fraction=0.995, min_iterations=500, max_iterations=50000):
    if lambda_per_hr <= 0:
        return max_iterations
    lambda_per_s = lambda_per_hr / 3600.0
    t = math.log(1.0 / (1.0 - target_fraction)) / lambda_per_s
    return int(min(max_iterations, max(min_iterations, round(t))))


def _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach):
    """Same shape as app._save_run_settings (report.py/paraview_launch.py
    read this regardless of whether the run came from a single Run click
    or a sweep), but saves the *actual* z/ach this subfolder ran with -
    settings itself still holds the base project's values, which would be
    wrong here for every combination but one.
    """
    data = dict(settings)
    data["z-value"] = z
    data["ach"] = ach
    data["guv_path"] = guv_path
    data["settings_path"] = settings_path
    data["monitoring_points"] = _gather_monitoring_points(settings)
    if settings.get("sim-type") == "steady_state":
        data["source_center"] = (
            settings.get("inject-x-input"), settings.get("inject-y-input"),
            settings.get("inject-z-input"),
        )
    write_case_file(case_dir, "run_settings.json", json.dumps(data, indent=2))


def _update_combo_status_safe(project_dir, project_name, z, ach, **fields):
    """update_combo_status, but never lets a bug/edge-case in this
    tracking feature break an actual production sweep - archival/
    bookkeeping only, same "never block the real work over it" contract
    as snapshot_openfoam_settings.
    """
    try:
        update_combo_status(project_dir, project_name, z, ach, **fields)
    except Exception:
        pass


def _update_ach_base_status_safe(project_dir, project_name, ach, flow_fingerprint, base_dir, control_dir,
                                  control_results, guv_path=None, settings_path=None, sim_type=None):
    """update_ach_base_status, but never lets a bug/edge-case in this
    tracking feature break an actual production sweep - same contract as
    _update_combo_status_safe above.
    """
    try:
        update_ach_base_status(project_dir, project_name, ach, flow_fingerprint, base_dir, control_dir,
                                control_results, guv_path=guv_path, settings_path=settings_path,
                                sim_type=sim_type)
    except Exception:
        pass


def _discard_stale_ach_scratch_if_mismatched(project_dir, project_name, ach, flow_fingerprint, dirs, log_fn):
    """If this ACH already has a recorded flow_fingerprint (from an
    earlier sweep launch on this same project_dir) that does NOT match
    the current one, the flow-affecting settings changed since then - any
    of `dirs` still on disk (typically _base_ACH*/_control_ACH*, and for
    steady-state _phase1_ACH* too) are known stale, not just unvalidated,
    so discard them here before _build_flow_base/_run_shared_control/
    _run_shared_phase1 get a chance to wrongly reuse them by file presence
    alone - none of their own reuse checks are fingerprint-aware, which is
    exactly what makes blindly trusting file presence across separate
    sweep launches unsafe without this.

    If there's no recorded fingerprint for this ACH at all (a project
    from before this feature existed, or its first-ever build at this
    ACH), this is a no-op - an unfingerprinted directory might still be
    perfectly valid, and it isn't this function's place to guess either
    way.
    """
    try:
        record = get_ach_base_record(project_dir, project_name, ach)
    except Exception:
        return
    if record is None or record.get("flow_fingerprint") == flow_fingerprint:
        return
    log_fn("Flow-affecting settings changed since the last run at this ACH - discarding "
           "the stale shared scratch directories before rebuilding...")
    existing = [d for d in dirs if Path(d).exists()]
    if existing:
        quoted = " ".join(f'"{wsl_path(d)}"' for d in existing)
        run_wsl_or_raise(f"rm -rf {quoted}", wsl_path(project_dir), "discarding stale shared scratch dirs")


def _find_done_combo_case_dir_for_ach(project_dir, project_name, ach, flow_fingerprint, sim_type,
                                        original_sim_type):
    """The case_dir of an existing "done" combo at this ACH whose own
    recorded flow_fingerprint matches, or None - a fallback flow-base
    source for build_ach_fn when no dedicated _base_ACH*/ach_bases record
    survives (see _seed_ach_base_from_existing_combo's own docstring for
    why this is a physically valid source, not an approximation): every Z
    sharing an ACH has an identical flow field baked into its own combo
    directory, that's the whole premise this module's ACH-major reuse
    already optimizes around - it just isn't pre-extracted into a
    dedicated scratch folder the way a fresh sweep's own base_dir is.

    sim_type/original_sim_type: only a combo whose OWN recorded sim_type
    matches this sweep's sim_type is a valid donor (2026-08-12) - a
    steady-state combo's case_dir is a copy of Phase 1's own case, which
    can carry a non-zero warm-started T field (see
    run_steady_state_scenario's phase1_t_initial) that
    _seed_ach_base_from_existing_combo's stripping does NOT remove (it
    only strips postProcessing/0/kUV/solved time-directories, not 0/T
    itself) - seeding a Decay-mode base from that would start the decay
    solve from already-contaminated air instead of the clean flow field
    it needs. A combo written before per-combo sim_type tracking existed
    has no "sim_type" of its own recorded - original_sim_type (the
    project's own first-ever recorded mode, itself immutable once set -
    see load_project_status's sim_type.setdefault) is the correct
    fallback for those: mode-switching is a brand-new capability, so any
    combo predating it can only ever have been run in whatever single
    mode this project has always used. The genuinely mode-agnostic reuse
    path (find_reusable_ach_base's own ach_bases record + surviving
    _base_ACH* scratch dir) is unaffected by this restriction - it never
    carries any Z-specific baggage in the first place, so it's always
    safe to reuse across sim_type/guv-design changes; this restriction
    only applies to this specific fallback.
    """
    try:
        status = load_project_status(project_dir, project_name)
    except Exception:
        return None
    for combo in status.get("combos", {}).values():
        if (combo.get("ach") == ach and combo.get("status") == "done"
                and combo.get("flow_fingerprint") == flow_fingerprint and "z" in combo
                and combo.get("sim_type", original_sim_type) == sim_type):
            # combo["subdir"], if recorded, is this combo's OWN actual
            # folder name - use it verbatim rather than recomputing from
            # (z, ach) alone, which would silently drop this combo's own
            # combo_suffix (see _subdir_name's own docstring) and point at
            # the wrong - possibly a different design's/mode's - folder.
            # Falls back to the old recompute for combos written before
            # this field existed (never suffixed themselves, so it's
            # correct for them too).
            subdir = combo.get("subdir") or _subdir_name(combo["z"], combo["ach"])
            return f"{project_dir}/{subdir}"
    return None


def _seed_ach_base_from_existing_combo(source_case_dir, base_dir, log_fn):
    """Copy an existing, completed combo's own case_dir into base_dir and
    strip that combo's Z-specific leftovers - its own postProcessing
    output, 0/kUV, and any solved time-directory beyond 0/ - so it can
    stand in for a dedicated flow-base scratch folder (see build_ach_fn
    and _find_done_combo_case_dir_for_ach above). _apply_z rewrites
    cellZones/fvOptions/0/kUV from scratch regardless of what's already
    there when it runs against this seeded base for the NEW Z, so only
    the leftovers it does NOT itself touch need stripping here - after
    this, base_dir has exactly the shape _build_flow_base's own existing
    "already flow-converged, reuse it" branch (0/fluenceRate present)
    expects, so that branch fires naturally without any special-casing on
    build_ach_fn's side.
    """
    source_wsl, base_wsl = wsl_path(source_case_dir), wsl_path(base_dir)
    parent_wsl = wsl_path(str(Path(base_dir).parent))
    log_fn(f"  Found an already-converged flow field inside an existing, completed "
           f"combo ({Path(source_case_dir).name}/) for this ACH - reusing it as the "
           f"shared flow base instead of re-meshing/re-converging from scratch...")
    run_wsl_or_raise(
        f'rm -rf "{base_wsl}" && cp -r "{source_wsl}" "{base_wsl}" && cd "{base_wsl}" && '
        f'rm -rf postProcessing "0/kUV" && '
        f'find . -maxdepth 1 -regextype posix-extended -regex "\\./[0-9]+(\\.[0-9]+)?" -exec rm -rf {{}} +',
        parent_wsl, "seeding this ACH's flow base from an existing completed combo")


def _seed_ach_base_if_no_scratch_survives(project_dir, project_name, ach, flow_fingerprint, base_dir, log_fn,
                                           sim_type, original_sim_type):
    """Called only when find_reusable_ach_base already found nothing (no
    ach_bases record, or one that no longer resolves) - if base_dir
    itself doesn't already have a flow-converged 0/fluenceRate sitting in
    it either (i.e. _build_flow_base's OWN presence check has nothing to
    find), look for an already-done combo at this ACH to seed it from
    instead of paying for a full re-mesh - see _find_done_combo_case_dir_
    for_ach/_seed_ach_base_from_existing_combo above. A no-op (not an
    error) if neither exists - build_ach_fn's normal fresh-build path
    handles that case exactly as it always has.

    sim_type/original_sim_type: forwarded straight to
    _find_done_combo_case_dir_for_ach - see its own docstring for why a
    cross-mode donor isn't safe here even though the genuinely
    mode-agnostic ach_bases/scratch-dir reuse path (already ruled out by
    the time this is called) is unaffected.
    """
    if Path(f"{base_dir}/0/fluenceRate").exists():
        return  # _build_flow_base's own presence check will already reuse this
    donor = _find_done_combo_case_dir_for_ach(project_dir, project_name, ach, flow_fingerprint,
                                               sim_type, original_sim_type)
    if donor is not None:
        _seed_ach_base_from_existing_combo(donor, base_dir, log_fn)


# --- flow-field build/reuse ---

def _build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, should_stop, solver_log_fn,
                      should_pause=None, sealed=False, mechanical_ach_only=False, solve_semaphore=None):
    """setup_case() into base_dir at this ACH - the project's currently
    configured Z is used as a placeholder (every Z-dependent file this
    writes gets overwritten by _apply_z before any subfolder actually
    runs), exactly the same call app._run_steady_state makes for a single
    run, just targeting a temp directory.

    sealed: forwarded straight to setup_case - True for a decay-mode ACH<=0
    (sealed room, fan-only mixing) group; steady-state sweeps never pass
    this (see run_sweep's own upfront ach>0 validation).

    mechanical_ach_only: forwarded straight to setup_case - skips the
    fluence/UV pipeline entirely (see run_pipeline._finish_case_setup).
    Decay sweeps only, mirroring sealed above.

    solve_semaphore: forwarded straight to setup_case/converge_flow_field
    - caps real concurrent OpenFOAM processes across a whole sweep,
    decoupled from the orchestration thread pool's own size (see that
    parameter's docstring for the starvation bug this closes). A no-op on
    the reuse branch below (no solve happens there at all).

    If base_dir already has a resolved flow-convergence result on disk
    from an earlier attempt at this ACH (a sweep that got interrupted or
    paused further downstream, e.g. by Phase1ExtrapolationUndecided, OR
    seeded from an existing done combo - see
    _seed_ach_base_if_no_scratch_survives above), reuse it instead of
    re-meshing and re-running simpleFoam from scratch - confirmed as
    expensive, real wasted compute on a live run (flow convergence here
    can be as costly as Phase 1 itself). Detected via 0/fluenceRate, the
    same signal case_awaiting_flow_decision() already uses: only written
    by setup_case()'s own _finish_case_setup once flow convergence is
    fully resolved (converged, accepted via oscillation, or explicitly
    accepted by a user) - so a case that got interrupted mid-convergence
    correctly does NOT match this and falls through to a fresh
    setup_case() call below.

    ach_delivery is still measured on this path (not just left None) -
    check_ach_delivery only reads already-solved fields (a few seconds,
    no new solve - see its own docstring), so it's just as cheap to run
    against a reused/seeded base as a freshly-converged one, and skipping
    it here would silently drop a real, independent sanity check from
    every combo sharing this ACH (confirmed as a real gap: it went missing
    from a project's sweep summary the first time this reuse path fired
    for real, since the flow-affecting settings genuinely didn't change,
    so there was nothing wrong to report, just nothing computed either).

    fluenceRate IS recomputed here even on reuse (2026-08-11 fix), unlike
    every other reused field - see the loud warning at that call site for
    why: it's the one field on this path that's a function of guv_path's
    lamp positions/power, not of the flow-affecting settings
    flow_fingerprint gates reuse on. Confirmed as a real, silent
    correctness bug in production use: flow_fingerprint deliberately
    excludes lamp/UV settings (a genuinely correct choice for the flow
    field and UV-off control run, which really are lamp-independent), but
    that same reuse gate was ALSO letting a stale fluenceRate survive from
    whichever .guv file happened to build/seed this base first - every
    later Z sharing this ACH (see _apply_z's own "read back from the file
    the base build already wrote rather than recomputed" comment) baked
    in that first .guv's lamp field regardless of which .guv the CURRENT
    run actually asked for, silently reproducing the ORIGINAL design's
    numbers under a newer design's name with no error or warning at all.
    """
    if Path(f"{base_dir}/0/fluenceRate").exists():
        log_fn(f"Found an already flow-converged base case at {Path(base_dir).name}/ from an earlier "
               f"attempt - reusing it instead of re-meshing/re-converging.")
        # fluenceRate is purely geometric (room + lamp positions/power -
        # see fluence.compute_fluence_at_points's own docstring), so this
        # is cheap (no OpenFOAM solve) - but it's the one field on this
        # reuse path that actually depends on guv_path, so it must be
        # recomputed fresh from THIS run's room/guv_path, never trusted
        # from whatever .guv file originally built/seeded this base (see
        # this function's own docstring for the bug this closes).
        log_fn("  Recomputing fluence rate from this run's own .guv file (never trusted from an "
               "earlier design, even when the flow field itself is reused)...")
        fluence_points = read_cell_centers(base_dir, "0")
        fluence_values = compute_fluence_at_points(room, fluence_points)
        write_scalar_field(base_dir, "fluenceRate", fluence_values, read_boundary_patch_names(base_dir))
        has_inlet2 = bool(settings.get("inlet2-enable"))
        inlet_velocity, inlet2_velocity = resolve_case_inlet_velocities(
            base_dir, room, ach, adv["mesh-cell-size"],
            settings["inlet-wall"], _opening_center_frac(settings, "inlet", room),
            (settings["inlet-size-w"], settings["inlet-size-h"]),
            inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
            inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
            inlet2_center=_opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
            inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
            inlet2_diffuser_type=settings.get("inlet2-diffuser-type", "direct"),
            sealed=sealed,
        )
        if sealed:
            log_fn("Skipping ACH-delivery check - sealed room, no ventilation to measure.")
            ach_delivery = None
        else:
            outlet_patches = ("outlet", "outlet2") if settings.get("outlet2-enable") else ("outlet",)
            room_volume = room.x * room.y * room.z
            ach_delivery = check_ach_delivery(base_dir, room_volume, ach, outlet_patches=outlet_patches,
                                               log_fn=log_fn)
        return {"flow_converged": None, "ach_delivery": ach_delivery, "n_lamps": len(room.lamps), "reused": True,
                "inlet_velocity": inlet_velocity, "inlet2_velocity": inlet2_velocity}
    return setup_case(
        guv_path, base_dir, template_case_dir=_TEMPLATE_CASE_DIR,
        Z=settings["z-value"], ach=ach,
        inlet_wall=settings["inlet-wall"],
        inlet_center=_opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        outlet_wall=settings["outlet-wall"],
        outlet_center=_opening_center_frac(settings, "outlet", room),
        outlet_size=(settings["outlet-size-w"], settings["outlet-size-h"]),
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        flow_rel_tol=adv["flow-rel-tol"] / 100.0, flow_max_iterations=adv["flow-max-iterations"],
        momentum_relaxation=adv["momentum-relaxation"], scalar_relaxation=adv["scalar-relaxation"],
        # Deliberately NOT adaptive_t_relaxation here: this builds the ONE
        # shared per-ACH base, using settings["z-value"] as a mere
        # placeholder - the real per-combo Z (and therefore the real
        # kUV.max) isn't known yet, and _apply_z() below already applies
        # adaptive relaxation correctly, per-Z, on each combo's own copy.
        # Doing it here too would just bake in one arbitrary Z's value and
        # then silently override it per copy - harmless but confusing.
        scalar_transport_ncorr=adv["scalar-transport-ncorr"],
        scalar_transport_tolerance=adv["scalar-transport-tolerance"],
        max_co=adv["max-co"],
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn, should_pause=should_pause,
        sealed=sealed, mechanical_ach_only=mechanical_ach_only, solve_semaphore=solve_semaphore,
        **_fan_kwargs(settings),
        **_second_opening_kwargs(settings, "inlet2", room),
        **_second_opening_kwargs(settings, "outlet2", room),
    )


def _copy_base_case(base_dir, target_dir, log_fn):
    """cp -r the shared flow-converged base case into a fresh per-Z
    subfolder, over WSL (matches how every other case-directory operation
    in this codebase already goes through WSL commands rather than raw
    Windows file ops - much faster for a many-small-file mesh directory
    than crossing the Windows<->WSL 9P bridge file-by-file).
    """
    base_wsl = wsl_path(base_dir)
    target_wsl = wsl_path(target_dir)
    parent_wsl = wsl_path(str(Path(target_dir).parent))
    log_fn(f"  Copying converged base case into {Path(target_dir).name}/...")
    run_wsl_or_raise(f'rm -rf "{target_wsl}" && cp -r "{base_wsl}" "{target_wsl}"',
                      parent_wsl, "copying base case")


def _carve_breathing_inlet(case_dir, room, settings, adv, log_fn):
    """Carve the source cellZone and return the breathing-inlet velocity
    constraint fvOptions entry for it - or None when the feature is off.

    EVERY scenario modelling this room has to apply the same injection
    configuration: a breathing occupant is present whether or not the UV
    lamps are on, so the UV-off control and the decay runs need this
    exactly as much as steady-state's Phase 1/2 do. Leaving it out of the
    control is not a harmless omission - it silently measures the
    ventilation rate on a DIFFERENT flow field than the one Phase 2 ran on
    (the constraint measurably alters the flow: it roughly halves the
    per-200-iteration velocity drift, and the jet it creates can
    short-circuit toward an extract). The eACH formulas then combine the
    two as if they shared a flow field. See ANALYSIS_LOG.md 2026-08-30.

    The zone has to be carved HERE rather than relied upon: Phase 1 carves
    it anyway for the volumetric T source, but the control clones the flow
    base (which has no sourceZone at all - the base is ventilation-only),
    and the decay path's _apply_z rewrites cellZones from scratch. Both
    would otherwise leave the constraint pointing at a cellZone that
    doesn't exist.
    """
    velocity = breathing_inlet_velocity(settings)
    if velocity <= 0:
        return None   # 0 m/s IS the "no breathing inlet" case - no separate flag
    # No injection position means there is nowhere to put the jet. Every real
    # project has these (they are the source-geometry fields right above the
    # velocity one), but decay-only settings dicts and older/partial projects
    # may not - degrade to "no breathing inlet" rather than raising KeyError,
    # since velocity now defaults to 0.06 and this runs for every scenario.
    if not all(k in settings for k in ("inject-x-input", "inject-y-input", "inject-z-input")):
        return None
    write_source_topo_set_dict(
        case_dir,
        (settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        resolve_source_size(settings, adv["mesh-cell-size"], (room.x, room.y, room.z)),
        cell_size=adv["mesh-cell-size"], room_dims=(room.x, room.y, room.z))
    run_wsl_or_raise("topoSet -dict system/sourceTopoSetDict", wsl_path(case_dir),
                      "topoSet (source zone for the breathing inlet)")
    direction = breathing_inlet_direction(settings)
    log_fn(f"  Breathing inlet velocity constraint enabled "
           f"(U fixed to {velocity:g} m/s in sourceZone, direction={direction})")
    return breathing_inlet_velocity_constraint(zone_name="sourceZone", velocity_magnitude=velocity,
                                               direction=direction)


def _apply_z(case_dir, Z, nbins, fan_kwargs, log_fn, adaptive_t_relaxation=False, scalar_relaxation=0.7):
    """Recompute the Z-dependent files in an already flow-converged,
    freshly-copied case dir: kUV and cellZones.

    fluenceRate is purely geometric (room/lamp positions - not run through
    OpenFOAM at all), so it's read back from the file the base build
    already wrote rather than recomputed. UV fvOptions entries are
    deliberately NOT written here - steady_state_pipeline.
    run_steady_state_scenario() rebuilds them fresh from 0/kUV for both
    phases regardless (see its _uv_fvoptions_entries), so setup_case()'s
    own initial fvOptions write is never actually used for steady-state
    scenarios in the first place. bin_decay_rates is a deterministic
    function of (k_values, nbins) - the same call in both places - so the
    cellZones written here is guaranteed to match what
    run_steady_state_scenario derives from this same kUV field later.

    fan_kwargs: this case's _fan_kwargs(settings) result (or {}) - write_
    cellzones() above rewrites constant/polyMesh/cellZones from scratch,
    wiping any fan zone the base build carved, so it needs re-carving
    here too (topoSet on the existing mesh - cheap, no re-meshing).

    adaptive_t_relaxation: sweep mode's own point for this - unlike a
    single run (where setup_case()/_finish_case_setup applies it once,
    right after Z's own kUV field is first written), a sweep shares ONE
    flow-converged base across every Z in the ACH group and only applies
    each Z's own kUV field here, on that Z's own freshly-copied case_dir -
    so this is the earliest, and correct, point per-Z kUV.max is actually
    known in a sweep. Applying it in setup_case() instead (during
    _build_flow_base's own one-time build) would bake in whatever
    relaxation ONE placeholder Z happened to need, then have every other
    Z in the sweep silently inherit that same, likely-wrong value via
    _copy_base_case - confirmed as a real gap, not just a theoretical one.

    scalar_relaxation: applied (also on this Z's own freshly-copied
    case_dir, same reasoning as above) whenever adaptive_t_relaxation is
    False - the fixed/manual scalar-relaxation value to use instead.
    """
    patch_names = read_boundary_patch_names(case_dir)
    fluence_values = np.array(read_openfoam_scalar_field(f"{case_dir}/0/fluenceRate"))
    try:
        volumes = np.array(read_cell_volumes(case_dir, "0"))
    except (RuntimeError, OSError):
        # A shared base built before writeCellVolumes existed here has no
        # 0/V (it's a static geometric postProcess artifact, never solved
        # for, so nothing regenerates it later in the pipeline either) -
        # every such base is a uniform mesh anyway (local refinement is
        # newer than this), so equal weights give the exact same
        # (correct) answer as a real volume-weighted mean would.
        volumes = np.ones(len(fluence_values))
    log_fn(f"  Recomputing kUV for Z={Z}...")
    k_values = compute_inactivation_rate(fluence_values, Z)
    write_scalar_field(case_dir, "kUV", k_values, patch_names)

    eACH_values = compute_well_mixed_eACH(k_values)

    bin_idx, bin_repr = bin_decay_rates(k_values, nbins)
    write_cellzones(case_dir, bin_idx, nbins)

    if fan_kwargs:
        case_dir_wsl = wsl_path(case_dir)
        center = fan_kwargs["fan_center"]
        thickness = fan_kwargs["fan_disk_thickness"]
        p1 = (center[0], center[1], center[2] - thickness / 2)
        p2 = (center[0], center[1], center[2] + thickness / 2)
        log_fn("  Re-carving fan cellZone (cellZones was rewritten from scratch above)...")
        write_fan_topo_set_dict(case_dir, p1, p2, fan_kwargs["fan_disk_radius"])
        run_wsl_or_raise("topoSet -dict system/fanTopoSetDict", case_dir_wsl, "topoSet (restore fan zone)")

    result = {
        "fluence_mean": _volume_weighted_mean(fluence_values, volumes),
        "eACH_uv_well_mixed_mean": _volume_weighted_mean(eACH_values, volumes),
    }
    if adaptive_t_relaxation:
        kuv_max = float(k_values.max())
        adaptive_relax = compute_adaptive_scalar_relaxation(kuv_max)
        log_fn(f"  Adaptive T-relaxation: kUV.max={kuv_max:.4g} -> scalar-relaxation={adaptive_relax:.3g}...")
        set_relaxation_factors(case_dir, scalar_factor=adaptive_relax)
        result["adaptive_scalar_relaxation"] = adaptive_relax
    else:
        # Mirrors the adaptive branch above for the same reason (see this
        # function's own docstring): a sweep's Z10_ACH6 (etc.) case_dir is
        # freshly copied from the ACH group's ONE shared flow base, which
        # only ever has whatever scalar-relaxation was in effect when that
        # base happened to be built - without this, every Z in the sweep
        # silently inherits that one baked-in value regardless of its own
        # configured scalar_relaxation (confirmed as a real, live gap
        # 2026-08-27: two sweep runs configured for 0.3 and 0.7 both
        # silently solved at a stale, unrelated 0.5 inherited from the
        # shared base's own original build).
        set_relaxation_factors(case_dir, scalar_factor=scalar_relaxation)
    return result


def _run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                   status_fn=None, control_results_future=None, base_summary=None, should_pause=None,
                   solve_semaphore=None):
    """run_steady_state_scenario() with this combination's z/ach - same
    call app._run_steady_state makes for a single run.

    status_fn, if given, receives each phase's latest "Time = N" line to
    display in place instead of the scrolling log - see
    steady_state_pipeline.run_steady_state_scenario's own docstring.

    control_results_future: a Future (see _completed_future for the
    "value already in hand" case) resolving to the shared, once-per-ACH
    UV-off control's own result dict (see _run_shared_control) - its
    measured ventilation rate feeds run_steady_state_scenario's
    measured_ventilation_ach, which is more reliable than deriving one
    from Phase 1's own point-source buildup (see
    compute_corrected_eACH_uv_from_control's docstring). None falls back
    to the old Phase-1-derived method.

    Resolved inside run_steady_state_scenario's own call below, not
    eagerly here - control runs concurrently with Phase 1/Phase 2 now
    (2026-08-11), consumed purely for a post-hoc corrected-report
    subtraction (steady_state_pipeline.py's own measured_ventilation_ach
    handling, well after its simpleFoam solve already ran), never as a
    solver input - resolving any earlier would block Phase 2's own
    simpleFoam call from starting until control had already finished,
    silently serializing the two for no reason (the exact bug this
    docstring update fixes - confirmed directly via a test asserting
    Phase 2's own solve and control's actually overlap in wall-clock
    time, not just Phase 1's).

    base_summary: the shared flow base's own setup_case() summary (see
    _build_flow_base) - carries flow_converged/ach_delivery/n_lamps
    through to this combo's own results.json, the same way
    _run_decay_scenario already does for decay mode. Regression fixed
    2026-07-27: run_sweep's build_ach_fn previously called
    _build_flow_base() without capturing its return value at all, so
    steady-state sweep results never had these fields (confirmed on a
    real sweep: ach_delivery was silently None in every combo's
    results.json, while the identical decay-mode sweep had it) - only
    single, non-swept runs (app._run_steady_state) ever set them.
    """
    fan_entry = None
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=direction)

    room_volume = room.x * room.y * room.z
    cell_size = adv["mesh-cell-size"]
    openings = [(settings["inlet-wall"],
                 opening_actual_area(settings["inlet-wall"], room.x, room.y, room.z,
                                      _opening_center_frac(settings, "inlet", room),
                                      (settings["inlet-size-w"], settings["inlet-size-h"]), cell_size))]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    if has_inlet2:
        openings.append((settings["inlet2-wall"],
                          opening_actual_area(settings["inlet2-wall"], room.x, room.y, room.z,
                                               _opening_center_frac(settings, "inlet2", room),
                                               (settings["inlet2-size-w"], settings["inlet2-size-h"]), cell_size)))
    velocities = compute_inlet_velocities(ach, room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None
    has_outlet2 = bool(settings.get("outlet2-enable"))

    eACH_uv = z_summary.get("eACH_uv_well_mixed_mean", 0.0)
    deltat_adv = merge_project_deltat_settings(settings, adv)
    if deltat_adv["deltat-scaling-enabled"]:
        # _settling_iterations-based inflation and deltaT scaling solve the
        # same equation for opposite unknowns - composing them defeats
        # deltaT scaling's purpose (confirmed directly: at ACH=6 this
        # inflation alone already pushes a 1500-iteration budget past the
        # point deltaT would have needed to scale). Use the configured
        # budget as-is and let deltaT provide residence-time coverage.
        phase1_iterations = settings["phase1-iterations"]
        phase2_iterations = settings["phase2-iterations"]
    else:
        phase1_iterations = max(settings["phase1-iterations"], _settling_iterations(ach))
        phase2_iterations = max(settings["phase2-iterations"], _settling_iterations(ach + eACH_uv))
    phase1_delta_t, phase2_delta_t = resolve_phase_delta_ts(ach, eACH_uv, phase1_iterations, phase2_iterations,
                                                             deltat_adv)

    patches_to_monitor = ("outlet", "outlet2") if has_outlet2 else ("outlet",)
    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, ach, z,
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=REFERENCE_TARGET_T_SS,
        inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2,
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        inlet_wall=settings["inlet-wall"], inlet_center=_opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet2_diffuser_type=settings.get("inlet2-diffuser-type", "direct") if has_inlet2 else "direct",
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_center=_opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
        inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
        phase1_iterations=phase1_iterations,
        phase2_iterations=phase2_iterations,
        phase1_write_interval=adv["phase-write-interval"],
        phase2_write_interval=adv["phase-write-interval"],
        window_frac=settings.get("t-ss-window-frac") or 0.15,
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        source_size=resolve_source_size(settings, adv["mesh-cell-size"], (room.x, room.y, room.z)),
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        t_inf_check_interval=adv["phase-chunk-size"] if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        keep_all_timesteps=adv["keep-all-timesteps"],
        # Explicit, not left to run_steady_state_scenario's own default -
        # sweep mode has no resume UX for a Phase1ExtrapolationUndecided
        # pause at all (a stuck combination just permanently fails, see
        # run_sweep's own docstring), so this must never silently inherit
        # a True default regardless of what that default currently is.
        phase1_extrapolation_gate=adv["phase1-require-stable-extrapolation"],
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn, should_pause=should_pause,
        status_fn=status_fn,
        control_results_future=control_results_future,
        phase1_delta_t=phase1_delta_t, phase2_delta_t=phase2_delta_t, solve_semaphore=solve_semaphore,
        t_clamp_decay_multiplier=adv["t-clamp-decay-multiplier"] if adv["t-clamp-decay-enabled"] else None,
        phase1_tmax_multiplier=adv["phase1-tmax-multiplier"] if adv["t-clamp-decay-enabled"] else None,
        breathing_inlet_velocity=breathing_inlet_velocity(settings),
        breathing_inlet_dir=breathing_inlet_direction(settings),
    )
    result["fluence_mean"] = z_summary["fluence_mean"]
    result["eACH_uv_well_mixed"] = z_summary.get("eACH_uv_well_mixed_mean")
    # Records the actual applied value, not just the adaptive-t-relaxation
    # flag - see this project's adaptive-t-relaxation setting for why: a
    # reload of the same project recomputes an equivalent value from the
    # same inputs (deterministic given the same app version), but this is
    # what lets a specific FINISHED run's real relaxation be read back
    # directly, without needing to trust that recomputation matches.
    result["adaptive_scalar_relaxation"] = z_summary.get("adaptive_scalar_relaxation")
    result["flow_converged"] = (base_summary or {}).get("flow_converged")
    result["ach_delivery"] = (base_summary or {}).get("ach_delivery")
    result["n_lamps"] = (base_summary or {}).get("n_lamps")
    result["mechanical_mixing_efficiency_pct"] = mechanical_mixing_efficiency_pct(result)
    return result


def _run_shared_phase1(base_dir, phase1_dir, ach, room, settings, adv, log_fn, should_stop, solver_log_fn,
                        status_fn=None, should_pause=None, solve_semaphore=None):
    """Run Phase 1 ("source only, no UV") ONCE per ACH group, shared
    across every Z sharing that ACH - Phase 1's own physics (injection
    strength G/Su depend only on ach/target_T_ss/source geometry, none of
    which vary with Z) doesn't depend on Z at all, the same way decay
    mode's UV-off control doesn't (see _run_shared_control). Confirmed
    directly: a real sweep's log showed Phase 1 ("source only, no UV")
    restarting from scratch for every Z sharing an ACH, each paying its
    own full phase1_iterations cost for a result that would have been
    identical regardless of which Z it ran under.

    Clones base_dir (the shared, flow-converged case) into phase1_dir and
    runs run_steady_state_scenario() there with phase1_only=True - stops
    right after Phase 1 converges and writes its checkpoint (see that
    function's docstring), leaving phase1_dir with a converged Phase 1
    state (0/T, phase1_T.snapshot) and checkpoint file on disk.

    Every Z sharing this ACH then clones phase1_dir instead of base_dir
    (see run_sweep's run_z_fn) - run_steady_state_scenario's existing
    checkpoint-detection logic picks up the already-converged state there
    and skips straight to Phase 2, without any special-casing needed on
    its end. The one thing that copy loses that _apply_z's own cellZones
    rewrite would otherwise destroy anyway - the source cellZone Phase 1
    carved - has to be re-carved per Z after _apply_z runs (see run_z_fn),
    the same way _apply_z already re-carves the fan zone for the same
    reason.

    If phase1_dir already carries a resolved checkpoint from an earlier
    attempt (e.g. a sweep that got interrupted after Phase 1 finished but
    before every Z's Phase 2 completed), this is a no-op - every Z clone
    below will pick the checkpoint up itself via run_steady_state_
    scenario's own checkpoint-detection logic, so there's nothing to
    redo here. If it instead carries an UNDECIDED Phase 1 (phase1_pending.
    json but no checkpoint - e.g. a run that hit Phase1ExtrapolationUndecided
    and was never resumed), reuse that in-progress state rather than
    wiping it via _copy_base_case and paying for the full phase1_iterations
    budget again: resume with phase1_resume_decision="accept", which
    samples a small additional window from the current state and finalizes
    on it (see run_steady_state_scenario's own docstring for that
    parameter) - correct now that phase1_extrapolation_gate defaults to
    off, since CV-plateau-alone is the acceptance criterion either way.
    """
    if _read_phase1_checkpoint(phase1_dir) is not None:
        log_fn(f"=== ACH={ach}: found an already-converged Phase 1 checkpoint at "
               f"{Path(phase1_dir).name}/ from an earlier attempt - reusing it, nothing to redo ===")
        # fluenceRate is recomputed here too, not just trusted from
        # whatever .guv file originally built/seeded phase1_dir - see
        # _build_flow_base's own identical fix and docstring for the
        # incident this closes. That fix alone wasn't enough for
        # steady-state: every Z/ACH combo is cloned from phase1_dir (see
        # run_z_fn's own _copy_base_case call below), NOT from base_dir
        # directly, so a stale fluenceRate sitting here silently survived
        # regardless of how correctly base_dir's own copy got refreshed -
        # confirmed live: base_dir's fluenceRate was genuinely fresh after
        # a rebuild, n_lamps in the final report was even correct (it
        # comes from base_dir's own setup_case() summary), yet every
        # actual combo's own results were still byte-identical to the
        # ORIGINAL design's, because the one field that actually drives
        # the UV simulation was never touched.
        log_fn("  Recomputing fluence rate from this run's own .guv file (never trusted from an "
               "earlier design, even when Phase 1 itself is reused)...")
        fluence_points = read_cell_centers(phase1_dir, "0")
        fluence_values = compute_fluence_at_points(room, fluence_points)
        write_scalar_field(phase1_dir, "fluenceRate", fluence_values, read_boundary_patch_names(phase1_dir))
        return
    phase1_resume_decision = None
    if _read_phase1_pending(phase1_dir) is not None:
        log_fn(f"=== ACH={ach}: found an undecided Phase 1 attempt at {Path(phase1_dir).name}/ from an "
               f"earlier attempt (interrupted before a checkpoint was written) - resuming it instead of "
               f"starting over ===")
        phase1_resume_decision = "accept"
        # Same fix as the checkpoint-reuse branch above, for the same
        # reason - Phase 1's own solve never uses fluenceRate/kUV at all
        # (no UV sink term, "source only, no UV"), so resuming it doesn't
        # touch this field either; it's just carried forward into every
        # Z's own combo later, so it must reflect THIS run's .guv file
        # regardless of Phase 1's own convergence state.
        log_fn("  Recomputing fluence rate from this run's own .guv file (never trusted from an "
               "earlier design, even when resuming Phase 1's own in-progress convergence)...")
        fluence_points = read_cell_centers(phase1_dir, "0")
        fluence_values = compute_fluence_at_points(room, fluence_points)
        write_scalar_field(phase1_dir, "fluenceRate", fluence_values, read_boundary_patch_names(phase1_dir))
    else:
        _copy_base_case(base_dir, phase1_dir, log_fn)

    fan_entry = None
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=direction)

    room_volume = room.x * room.y * room.z
    cell_size = adv["mesh-cell-size"]
    openings = [(settings["inlet-wall"],
                 opening_actual_area(settings["inlet-wall"], room.x, room.y, room.z,
                                      _opening_center_frac(settings, "inlet", room),
                                      (settings["inlet-size-w"], settings["inlet-size-h"]), cell_size))]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    if has_inlet2:
        openings.append((settings["inlet2-wall"],
                          opening_actual_area(settings["inlet2-wall"], room.x, room.y, room.z,
                                               _opening_center_frac(settings, "inlet2", room),
                                               (settings["inlet2-size-w"], settings["inlet2-size-h"]), cell_size)))
    velocities = compute_inlet_velocities(ach, room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None
    has_outlet2 = bool(settings.get("outlet2-enable"))

    deltat_adv = merge_project_deltat_settings(settings, adv)
    if deltat_adv["deltat-scaling-enabled"]:
        phase1_iterations = settings["phase1-iterations"]
    else:
        phase1_iterations = max(settings["phase1-iterations"], _settling_iterations(ach))
    # Phase 1 alone has no UV/Z dependency, so eACH_uv_well_mixed=0 here -
    # phase2_delta_t is discarded (phase1_only=True below runs no Phase 2).
    phase1_delta_t, _ = resolve_phase_delta_ts(ach, 0.0, phase1_iterations, phase1_iterations, deltat_adv)
    patches_to_monitor = ("outlet", "outlet2") if has_outlet2 else ("outlet",)

    log_fn(f"=== ACH={ach}: Phase 1 (source only, no UV - once per ACH) ===")
    run_steady_state_scenario(
        # Z is a placeholder here (Phase 1 has no UV, so its value is
        # irrelevant) - same convention _build_flow_base's own docstring
        # already uses for the shared flow base.
        phase1_dir, room.x, room.y, room.z, ach, settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=REFERENCE_TARGET_T_SS,
        inlet_velocity=inlet_velocity, inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2,
        inlet_diffuser_type=settings.get("inlet-diffuser-type", "direct"),
        inlet_wall=settings["inlet-wall"], inlet_center=_opening_center_frac(settings, "inlet", room),
        inlet_size=(settings["inlet-size-w"], settings["inlet-size-h"]),
        inlet2_diffuser_type=settings.get("inlet2-diffuser-type", "direct") if has_inlet2 else "direct",
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_center=_opening_center_frac(settings, "inlet2", room) if has_inlet2 else None,
        inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
        phase1_iterations=phase1_iterations,
        phase1_write_interval=adv["phase-write-interval"],
        window_frac=settings.get("t-ss-window-frac") or 0.15,
        cell_size=adv["mesh-cell-size"],
        source_size=resolve_source_size(settings, adv["mesh-cell-size"], (room.x, room.y, room.z)),
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        t_inf_check_interval=adv["phase-chunk-size"] if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        keep_all_timesteps=adv["keep-all-timesteps"],
        phase1_extrapolation_gate=adv["phase1-require-stable-extrapolation"],
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn, should_pause=should_pause,
        status_fn=status_fn, phase1_only=True, phase1_delta_t=phase1_delta_t,
        phase1_resume_decision=phase1_resume_decision, solve_semaphore=solve_semaphore,
        t_clamp_decay_multiplier=adv["t-clamp-decay-multiplier"] if adv["t-clamp-decay-enabled"] else None,
        phase1_tmax_multiplier=adv["phase1-tmax-multiplier"] if adv["t-clamp-decay-enabled"] else None,
        breathing_inlet_velocity=breathing_inlet_velocity(settings),
        breathing_inlet_dir=breathing_inlet_direction(settings),
    )


# --- decay-mode scenario (mirrors app._decay_run_durations/_run_decay_pair/
# _finish_decay, but parametrized on the caller's log_fn/should_stop rather
# than the Dash app's globals - see module docstring for why this module
# doesn't import app.py) ---

def _decay_run_durations(ach, eACH_well_mixed_est, adv):
    """Same rule as app._decay_run_durations: the UV-off control run
    targets decay-ach-min-fraction alone (ventilation-only decay is often
    much slower than combined, so pushing it further can cost hours for
    little gain); the UV-on run targets decay-each-max-fraction when that's
    cheap (estimated time <= the same practical ceiling), else falls back
    to decay-each-min-fraction.
    """
    ceiling = 7200
    control_end_time = _settling_iterations(
        ach, target_fraction=adv["decay-ach-min-fraction"] / 100.0, max_iterations=ceiling)

    combined_rate = ach + eACH_well_mixed_est
    raw_each_max_time = _settling_iterations(
        combined_rate, target_fraction=adv["decay-each-max-fraction"] / 100.0, max_iterations=10 ** 9)
    each_target = (adv["decay-each-max-fraction"] if raw_each_max_time <= ceiling
                   else adv["decay-each-min-fraction"])
    combined_end_time = _settling_iterations(combined_rate, target_fraction=each_target / 100.0,
                                              max_iterations=ceiling)
    return combined_end_time, control_end_time


def _prefixed_log_fn(log_fn, prefix):
    """Wraps log_fn so every line it emits is tagged with which ACH group
    or Z/ACH combination it came from - necessary once multiple ACH
    groups/Z values can be solving concurrently (see _MAX_CONCURRENT_SOLVES's
    own docstring) and their log lines interleave; under the old strictly-sequential
    sweep this context was implicit (only one combination's lines were
    ever being produced at a time).
    """
    return lambda msg: log_fn(f"[{prefix}] {msg}")


def _throttled_solver_callback(log_fn, log_prefix, on_line=None, status_fn=None, status_key=None,
                                total_time=None):
    """Wraps a solver's on_line callback so only "Time = N" banner lines
    and run_wsl_streaming's own "[...]"-wrapped stall/retry diagnostics
    reach the visible log - the full per-iteration residual dump (~8-10
    lines per "Time =" line) never did even for a single run (it only
    ever fed a silent progress tracker), so forwarding all of it here too
    would flood the log (see app._run_decay_pair for the same reasoning).

    status_fn(status_key, line), if given, receives "Time = N" banner
    lines INSTEAD of log_fn - a one-entry-per-stream "latest time" status
    the caller can overwrite in place (see app._scenario_status_update)
    rather than appending to the scrolling log, since with several
    concurrent ACH/Z combinations each printing their own "Time = N" line
    every step, appending all of them would flood the log exactly the way
    this function already avoids doing for the raw per-iteration residual
    dump. "[...]" diagnostics always still go to log_fn - those are rare
    and important enough to want scrolling, not overwritten away.

    total_time: this run's own target end time [s] (control_end_time or
    combined_end_time - whatever the caller computed BEFORE launching the
    solve, the same value set_control_dict_time's end_time was given) -
    appended as "Time = N of total_time total seconds" so status_fn's
    display always shows a comparable elapsed-vs-total pair, in the same
    units, for every run - decay/control, single-run or sweep, alike.
    None (rare - only if a caller genuinely doesn't have this value
    handy) just omits the suffix rather than raising.

    The status_fn branch deliberately does NOT prefix the line with
    log_prefix - status_key already carries that (and the combo's Z/ACH
    identity too), and the caller renders "[{key}] {value}" itself (see
    app._poll_scenario) - prefixing here too would just double it up.
    """
    def callback(line):
        stripped = line.strip()
        if _TIME_LINE_RE.match(stripped):
            if status_fn is not None:
                display = f"{stripped} of {total_time} total seconds" if total_time is not None else stripped
                status_fn(status_key, display)
            else:
                log_fn(f"[{log_prefix}] {line}")
        elif stripped.startswith("["):
            log_fn(f"[{log_prefix}] {line}")
        if on_line:
            on_line(line)
    return callback


def _run_shared_control(base_dir, control_dir, ach, room, settings, adv, log_fn, should_stop, solver_log_fn,
                         base_summary, status_fn=None, should_pause=None, sealed=False, solve_semaphore=None):
    """Run the UV-off control decay ONCE per ACH group, shared across
    every Z sharing that ACH - control's own physics (uniform T=1 initial
    condition, no UV sink, same converged flow field) doesn't depend on Z
    at all, confirmed directly: four different-Z combinations sharing an
    ACH group produced byte-identical control decay curves. The original
    per-Z design re-ran this (often the LONGER of the pair - ventilation-
    only decay is usually slower than combined decay) once per Z instead
    of once per ACH, wasting most of its cost N-1 times over for N Z
    values sharing that ACH.

    base_dir: the shared, flow-converged (not yet Z-specific) case this
    ACH group's own _build_flow_base built - cloned here before any Z's
    own UV fvOptions get applied, so this control run is genuinely
    Z-independent from the start, not just coincidentally so.

    solve_semaphore: if given, acquired only around the pimpleFoam
    invocation below (not held while the caller later awaits this future's
    result elsewhere) - see run_pipeline.converge_flow_field's identical
    parameter docstring for the starvation bug this closes.

    base_summary: base_dir's own setup_case() summary (from
    _build_flow_base) - its inlet_velocity/inlet2_velocity are reused
    directly rather than recomputed from nominal opening size (see
    ventilation_control.prepare_ventilation_only_control's docstring).
    """
    control_dir_wsl = wsl_path(control_dir)
    _, control_end_time = _decay_run_durations(ach, 0.0, adv)
    write_interval = max(1, settings["pimple-write-interval"])
    has_inlet2 = bool(settings.get("inlet2-enable"))

    log_fn(f"=== ACH={ach}: preparing shared UV-off control ({control_end_time}s, "
           f"once per ACH) ===")
    # Built before the clone (it's pure text, needs no case dir) so the
    # fvOptions prepare_ventilation_only_control writes already carries it;
    # the cellZone it binds to is carved right after, once control_dir exists.
    _bv = breathing_inlet_velocity(settings)
    breathing_entry = (breathing_inlet_velocity_constraint(
                           zone_name="sourceZone", velocity_magnitude=_bv,
                           direction=breathing_inlet_direction(settings))
                       if _bv > 0 else None)
    prepare_ventilation_only_control(
        base_dir, control_dir, base_summary["inlet_velocity"],
        control_end_time, write_interval, pimple_delta_t=adv["pimple-delta-t"], max_co=adv["max-co"],
        inlet2_velocity=base_summary.get("inlet2_velocity") if has_inlet2 else None,
        has_outlet2=bool(settings.get("outlet2-enable")),
        sealed=sealed,
        log_fn=log_fn, should_stop=should_stop,
        breathing_entry=breathing_entry,
    )
    if breathing_entry is not None:
        # The base this was cloned from is ventilation-only and has no
        # sourceZone, so the constraint's target zone must be carved here.
        _carve_breathing_inlet(control_dir, room, settings, adv, log_fn)

    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    log_fn(f"  Running pimpleFoam (shared control, {control_end_time}s)...")
    status_key = f"ACH={ach}/control"
    try:
        with solve_semaphore or contextlib.nullcontext():
            r = run_wsl_streaming(
                "pimpleFoam 2>&1 | tee log.pimpleFoam", control_dir_wsl,
                on_line=_throttled_solver_callback(log_fn, "control", status_fn=status_fn, status_key=status_key,
                                                    total_time=control_end_time),
                should_stop=should_stop, kill_pattern="pimpleFoam", should_pause=should_pause,
            )
    finally:
        if status_fn is not None:
            status_fn(status_key, None)
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"Shared UV-off control pimpleFoam failed (exit {r.returncode}):\n{tail}")

    log_fn("  Post-processing shared UV-off control...")
    return finish_ventilation_only_control(control_dir, ach, log_fn=log_fn)


def _run_decay_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                         control_results_future, base_summary=None, status_fn=None, should_pause=None,
                         solve_semaphore=None):
    """Decay-mode equivalent of _run_scenario() - runs this combination's
    own UV-on decay (adaptive duration) and writes the same corrected
    results.json shape a single decay run would, using control_results
    (the shared, once-per-ACH UV-off control - see _run_shared_control)
    instead of running its own redundant copy.

    Takes a Future (see _completed_future for the "value already in hand"
    case), not a resolved dict, and only calls .result() on it right
    before write_results_summary actually needs it - AFTER this combo's
    own pimpleFoam solve below has already run. Control genuinely runs
    concurrently with this combo's own decay solve (2026-08-11 - control
    is consumed purely for a post-hoc subtraction, never as a solver
    input), so resolving any earlier would silently serialize the two
    pimpleFoam runs behind each other for no reason - confirmed directly:
    an earlier version resolved the future before this function was even
    called, which meant this combo's own solve never actually started
    until control's had already finished.

    Unlike steady-state's _run_scenario, this doesn't need
    run_steady_state_scenario itself - but it DOES need to rebuild the UV
    fvOptions fresh from this combination's own 0/kUV field, the same way
    run_steady_state_scenario does via _uv_fvoptions_entries. _apply_z()
    (called by the caller before this) only rewrites the kUV *field* and
    cellZones, deliberately NOT the fvOptions splice itself (see its own
    docstring) - it assumes the caller rebuilds that, matching what
    run_steady_state_scenario already does for the steady-state sweep.
    An earlier version of this function assumed setup_case()'s own
    one-time fvOptions write (done once per ACH group, using whatever Z
    the shared flow base happened to be built with) was still valid here
    - it wasn't: every Z sharing an ACH group silently reused that first
    Z's actual UV removal rate in the solver, regardless of its own kUV
    field being correctly recomputed on disk (confirmed directly: two
    different-Z combinations produced byte-identical decay curves).

    solve_semaphore: if given, acquired only around this combo's own
    pimpleFoam invocation below - released well before the
    control_results_future.result() wait further down, which must never
    hold it (see this function's own docstring above for why that wait
    happens last) - see run_pipeline.converge_flow_field's identical
    parameter for the starvation bug this closes.
    """
    case_dir_wsl = wsl_path(case_dir)

    has_fan = bool(settings.get("fan-enable"))
    fan_entries = []
    if has_fan:
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_entries = [fan_fvoptions_entry(settings["fan-speed"], direction=direction)]
    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    uv_entries = _uv_fvoptions_entries(np.array(k_values), adv["uv-zone-bins"])
    # Same injection configuration as every other scenario - the occupant
    # is breathing here too. _apply_z rewrote cellZones just above, so the
    # helper re-carves sourceZone before the constraint binds to it.
    breathing_entry = _carve_breathing_inlet(case_dir, room, settings, adv, log_fn)
    breathing_entries = [breathing_entry] if breathing_entry is not None else []
    write_fvoptions_file(case_dir, uv_entries + fan_entries + breathing_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch after UV fvOptions splice: open={n_open} close={n_close}"

    eACH_well_mixed_est = z_summary.get("eACH_uv_well_mixed_mean", 0.0)
    combined_end_time, _ = _decay_run_durations(ach, eACH_well_mixed_est, adv)
    write_interval = max(1, settings["pimple-write-interval"])
    log_fn(f"  Adaptive duration: UV-on={combined_end_time}s, write interval={write_interval}s "
           f"(as configured) - UV-off control is shared for this ACH, not re-run here...")
    set_control_dict_time(case_dir, end_time=combined_end_time,
                           write_interval=write_interval, delta_t=adv["pimple-delta-t"], max_co=adv["max-co"])
    splice_live_vol_average_if_needed(case_dir)

    if adv["t-clamp-decay-enabled"]:
        # Decay mode carves no source zone (T starts uniform at
        # REFERENCE_TARGET_T_SS with no injection during the UV-on run,
        # only removal) - the physical ceiling is that known starting
        # value itself, not a converged-field lookup like steady-state's
        # source_zone_max_T (see tclamp_decay.py's module docstring).
        ensure_tclamp_decay_compiled(log_fn)
        splice_tclamp_decay_if_needed(case_dir, adv["t-clamp-decay-multiplier"] * REFERENCE_TARGET_T_SS)

    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    log_fn(f"  Running pimpleFoam (UV-on, {combined_end_time}s)...")
    status_key = f"Z={z}/ACH={ach}/UV-on"
    try:
        with solve_semaphore or contextlib.nullcontext():
            r_uv = run_wsl_streaming(
                "pimpleFoam 2>&1 | tee log.pimpleFoam", case_dir_wsl,
                on_line=_throttled_solver_callback(log_fn, "UV-on", solver_log_fn,
                                                    status_fn=status_fn, status_key=status_key,
                                                    total_time=combined_end_time),
                should_stop=should_stop, kill_pattern="pimpleFoam", should_pause=should_pause,
            )
    finally:
        if status_fn is not None:
            status_fn(status_key, None)
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    if r_uv.returncode != 0 or "FOAM FATAL" in r_uv.stdout or "Floating Point Exception" in r_uv.stdout:
        tail = "\n".join(r_uv.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"UV-on pimpleFoam failed (exit {r_uv.returncode}):\n{tail}")

    log_fn("  Computing spatial coefficient of variation (final concentration field, "
           "across all cells - how uniformly the room actually cleared, not just on average)...")
    try:
        spatial_cov = spatial_coefficient_of_variation(read_latest_time_field(case_dir, "T"))
    except Exception as e:
        log_fn(f"  Could not compute spatial CoV: {e}")
        spatial_cov = None

    # Resolved here, at the point of actual use - only needed for this
    # post-hoc subtraction, never as an input to the pimpleFoam solve
    # above, so this is the latest point (not the earliest) this combo's
    # own thread ever blocks waiting for control - see this function's
    # own docstring for why that ordering matters.
    control_results = control_results_future.result()
    log_fn("  Writing results summary...")
    result = write_results_summary(
        case_dir, f"{case_dir}/results.json", ach, eACH_well_mixed_est,
        extra={
            "fluence_mean": z_summary.get("fluence_mean"),
            "flow_converged": (base_summary or {}).get("flow_converged"),
            "ach_delivery": (base_summary or {}).get("ach_delivery"),
            "n_lamps": (base_summary or {}).get("n_lamps"),
            "spatial_cov_final": spatial_cov,
            "adaptive_scalar_relaxation": z_summary.get("adaptive_scalar_relaxation"),
        },
        measured_ventilation_ach=control_results["total_ach_effective"],
        measured_ventilation_ach_ci95=control_results.get("total_ach_effective_ci95"),
        measured_ventilation_ach_se_per_s=control_results.get("fit_se_per_s"),
        measured_ventilation_fit_dof=(control_results["fit_n"] - 2) if control_results.get("fit_n") else None,
    )

    points = _gather_monitoring_points(settings)
    if points:
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before monitoring locations.")
        log_fn("  Computing monitoring locations...")
        result["monitoring"] = compute_monitoring_results(
            case_dir, points, cell_size=adv["mesh-cell-size"], ventilation_ach=ach, log_fn=log_fn)
        write_case_file(case_dir, "results.json", json.dumps(result, indent=2))
    return result


def _trim_decay_report(result):
    """Decay-mode equivalent of _trim_report() - strips the bulky
    per-iteration decay_curve arrays (top-level and each monitoring
    point's), keeping every scalar result (eACH_uv figures, mixing
    efficiency, measured ACH, ...).
    """
    trimmed = dict(result)
    trimmed.pop("decay_curve", None)
    monitoring = trimmed.get("monitoring")
    if monitoring:
        trimmed_monitoring = {}
        for name, point in monitoring.items():
            point = dict(point)
            point.pop("t_seconds", None)
            point.pop("volAverage_T", None)
            trimmed_monitoring[name] = point
        trimmed["monitoring"] = trimmed_monitoring
    return trimmed


_SWEEP_SUMMARY_FIELDS = ["Z", "ACH", "Design", "Mode", "total_reduction_pct", "ach_efficiency_pct",
                         "uv_efficiency_pct", "mechanical_mixing_efficiency_pct", "est_ach_per_hr",
                         "est_each_per_hr", "ach_t_measured_per_hr", "phase1_spatial_cov_pct",
                         "phase2_T_ss_cv_pct", "phase1_converged", "phase2_converged"]


def _convergence_quality_columns(detail):
    """phase1_spatial_cov_pct/phase2_T_ss_cv_pct/phase1_converged/
    phase2_converged - convergence-quality diagnostics for the sweep
    summary CSV (2026-08-13), steady-state only (decay-mode reports have
    no phase1/phase2 structure, so all four come back None for a decay
    row - csv.DictWriter leaves them blank, not an error).

    The *_cov_pct/*_cv_pct pair are two DIFFERENT kinds of variability,
    not the same number twice - see
    decay_analysis.spatial_coefficient_of_variation's own docstring for
    the full contrast:
    - phase1_spatial_cov_pct: SPATIAL - how uniformly mixed Phase 1's
      final T field is across cells (a snapshot at the last iteration).
    - phase2_T_ss_cv_pct: TEMPORAL - how much the room-average T itself
      still fluctuates over Phase 2's own trailing convergence window
      (steady_state_pipeline's detrended windowed_stats).
    Percentages, matching how report.py's own steady-state table already
    displays both.

    phase1_converged/phase2_converged (2026-08-17): steady_state_pipeline
    already computes this (check_plateau_windowed, on the RAW, non-
    detrended CV - genuinely different from phase2_T_ss_cv_pct above,
    which is detrended specifically for "how noisy is this" reporting,
    not plateau detection) and stores it in every report - but until now
    nothing surfaced it anywhere a user would actually see it across a
    whole sweep, only a passive text label buried in the single-run docx
    report. Confirmed as a real incident: a run whose Phase 1 AND Phase 2
    both never actually plateaued (a persistent ~15-20% oscillation, no
    sign of damping after running its full iteration budget) still
    produced a clean, confident-looking reduction_pct with no visible
    caveat anywhere. False (not blank/None) when a phase genuinely ran
    and didn't converge - a sweep can now be scanned for this directly,
    not just spot-checked case by case.
    """
    phase1 = detail.get("phase1") or {}
    phase2 = detail.get("phase2") or {}
    spatial_cov1 = phase1.get("spatial_cov")
    t_ss_cv2 = phase2.get("T_ss_cv")
    return {
        "phase1_spatial_cov_pct": spatial_cov1 * 100 if spatial_cov1 is not None else None,
        "phase2_T_ss_cv_pct": t_ss_cv2 * 100 if t_ss_cv2 is not None else None,
        "phase1_converged": phase1.get("converged"),
        "phase2_converged": phase2.get("converged"),
    }


def _monitoring_summary_columns(detail):
    """One column per configured monitoring point's own headline metric -
    "{name}_reduction_pct" (steady-state, via point_reduction_basis on its
    phase1/phase2 entries - the same computation report.py/app.py already
    use to show per-point reduction) or "{name}_eACH_uv" (decay,
    monitoring_points.compute_monitoring_results already computes this
    directly per point) - added to the sweep summary CSV alongside the
    room-average metrics combo_summary_metrics provides. A point missing
    the data it needs (e.g. only one phase present) is just omitted from
    the returned dict for that row, not an error - csv.DictWriter leaves
    a row's missing fieldnames blank rather than failing.
    """
    monitoring = detail.get("monitoring") or {}
    is_steady_state = "reduction_pct" in detail or "reduction_pct_corrected" in detail
    columns = {}
    for name, point in monitoring.items():
        if is_steady_state:
            p1, p2 = point.get("phase1"), point.get("phase2")
            if p1 and p2:
                try:
                    _, _, reduction_pct, _ = point_reduction_basis(p1, p2)
                    columns[f"{name}_reduction_pct"] = reduction_pct
                except Exception:
                    pass
        elif point.get("eACH_uv_effective") is not None:
            columns[f"{name}_eACH_uv"] = point["eACH_uv_effective"]
    return columns


def write_sweep_summary_csv(project_dir, project_name):
    """Collects every DONE combination's trimmed per-combo report.json
    (already written by run_sweep/run_decay_sweep next to results.json -
    see _trim_report/_trim_decay_report) into one combined CSV with the
    same 5 headline numbers shown on the Run Simulations tab (report.
    combo_summary_metrics), plus one column per configured monitoring
    point's own headline metric (see _monitoring_summary_columns) - so
    comparing combinations doesn't require opening every subfolder's own
    results.json by hand.

    Reads the FULL set of combos from project_status.json rather than
    being handed a specific list by the caller - a later, narrower sweep
    (e.g. "add one more Z" via the Extend/modify modal, covering only one
    ACH) must not silently overwrite this summary down to just that
    call's own combos, dropping every previously-recorded row not part of
    it (confirmed as a real incident: a 9-row summary got overwritten
    down to 3 rows by a follow-up sweep that only touched one ACH).
    Combinations that never produced a report.json (failed, skipped, or
    not yet reached) are simply omitted, not written as blank rows.
    Returns the CSV's path.
    """
    try:
        status = load_project_status(project_dir, project_name)
    except Exception:
        status = {"combos": {}}
    # Iterates the combo RECORDS themselves (keyed by _combo_key, which
    # includes each combo's own combo_suffix - see compute_guv_design_suffix/
    # compute_sim_type_suffix) rather than a deduplicated {(z, ach), ...}
    # set - two different .guv designs (or sim-types) can both have a
    # "done" combo at the same Z/ACH, and deduplicating by (z, ach) alone
    # used to silently collapse them into one row (whichever one's own
    # report.json _subdir_name(z, ach)
    # happened to reconstruct without a suffix - i.e. always the
    # ORIGINAL design's), dropping every other design's row entirely.
    combos = sorted(
        ((key, c) for key, c in status.get("combos", {}).items()
         if c.get("status") == "done" and "z" in c and "ach" in c),
        key=lambda kv: (kv[1]["ach"], kv[1]["z"], kv[0]),
    )

    rows = []
    monitoring_columns = set()
    for key, combo in combos:
        z, ach = combo["z"], combo["ach"]
        # combo["subdir"], if recorded, is this combo's OWN actual folder
        # name - see _find_done_combo_case_dir_for_ach's identical
        # fallback for why recomputing _subdir_name(z, ach) alone isn't
        # safe once a combo may belong to a non-original design.
        subdir = combo.get("subdir") or _subdir_name(z, ach)
        report_relative = f"{project_name}_{subdir}_report.json"
        try:
            detail = json.loads(_read_case_file(project_dir, report_relative))
        except (json.JSONDecodeError, OSError, RuntimeError):
            # Missing (failed/skipped/not-yet-reached combo), unreadable, or
            # malformed - read via _read_case_file (not a plain Windows-side
            # Path.exists()+open()) since this file was written by a
            # WSL-native process (see write_case_file) and a Windows-side
            # existence check on it isn't reliable - see wsl_utils's own
            # cross-boundary-visibility docstrings.
            continue
        design = Path(combo["guv_path"]).stem if combo.get("guv_path") else ""
        mode = combo.get("sim_type") or ""
        row = {"Z": z, "ACH": ach, "Design": design, "Mode": mode,
               **combo_summary_metrics(detail), **_convergence_quality_columns(detail),
               **_monitoring_summary_columns(detail)}
        monitoring_columns.update(row.keys() - set(_SWEEP_SUMMARY_FIELDS))
        rows.append(row)

    fieldnames = _SWEEP_SUMMARY_FIELDS + sorted(monitoring_columns)
    csv_relative = f"{project_name}_sweep_summary.csv"
    csv_path = f"{project_dir}/{csv_relative}"
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    write_case_file(project_dir, csv_relative, buf.getvalue())
    return csv_path


def _completed_future(value):
    """A Future already resolved to `value` - lets a caller that sometimes
    has a real background task (e.g. a control run) and sometimes has a
    value already in hand (e.g. a sealed room's fixed 0.0 decay rate, or
    an ACH's reused control_results) hand BOTH cases to run_z_fn as the
    exact same "control_results_future" shape, with no special-casing at
    the point of use (control_results_future.result())."""
    future = Future()
    future.set_result(value)
    return future


def _run_ach_pool(achs, ach_worker, should_stop):
    """Run ach_worker(ach) for every ach on its own thread (unbounded - see
    _MAX_CONCURRENT_SOLVES's own docstring for why the REAL concurrency cap
    lives on the shared solver pool each ach_worker submits its actual
    OpenFOAM-invoking work to, not here: these per-ACH orchestrator threads
    mostly just call .submit()/.result() and sit blocked waiting, they
    don't themselves consume a core). Re-raises the first exception hit by
    any ach_worker (StoppedByUser or otherwise) once every already-running
    worker has settled - matches a plain ThreadPoolExecutor context
    manager's own shutdown(wait=True) semantics, just without capping
    how many run at once.
    """
    with ThreadPoolExecutor(max_workers=max(1, len(achs))) as ach_pool:
        ach_futures = [ach_pool.submit(ach_worker, ach) for ach in achs]
        for f in as_completed(ach_futures):
            f.result()


def _skip_if_combo_already_done(case_dir, subdir, combo_log_fn, trim_fn, on_combo_done, z, ach):
    """True (and reports "done" via on_combo_done using the existing
    result) if this combination already has a results.json on disk from an
    earlier attempt at this same project_dir - e.g. the app was closed/
    restarted mid-sweep, or a sweep was cancelled partway through.
    Re-launching a sweep should pick up where it left off instead of
    silently redoing every already-completed combination from scratch.

    False (caller should run this combo normally) if there's no
    results.json yet, or if the one that's there fails to parse - a
    combination that isn't actually finished on disk always just re-runs,
    the same as a fresh sweep would.
    """
    try:
        content = _read_case_file(case_dir, "results.json")
    except Exception as e:
        if isinstance(e, FileNotFoundError) or "No such file" in str(e):
            return False  # no results.json yet - first attempt at this combo, nothing to report
        combo_log_fn(f"  {subdir} has a results.json from an earlier attempt, but it couldn't be "
                     f"read ({e}) - re-running from scratch.")
        return False
    try:
        result = json.loads(content)
        trimmed = trim_fn(result)
    except Exception as e:
        combo_log_fn(f"  {subdir} has a results.json from an earlier attempt, but it couldn't be "
                     f"read ({e}) - re-running from scratch.")
        return False
    combo_log_fn(f"  {subdir} already completed in an earlier attempt - skipping "
                 f"(delete {subdir}/ to force a re-run).")
    if on_combo_done:
        on_combo_done(z, ach, "done", trimmed)
    return True


def _iter_combo_dirs(project_dir):
    """Every immediate subfolder of project_dir that looks like a combo
    directory (see _subdir_name - "Z<...>_ACH<...>") - used by the status-
    file-rebuild helpers below, not by the sweep functions themselves
    (which always know their own combo dirs directly).
    """
    base = Path(project_dir)
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and entry.name.startswith("Z"):
            yield entry


def find_first_guv_path_on_disk(project_dir):
    """The first guv_path found in any combo subfolder's own
    run_settings.json under project_dir, or None if none have one -
    used to resolve a Project/room for rebuild_project_status_from_disk
    when a project directory predates project_status.json entirely (so
    there's no status file to read guv_path from directly), but its own
    combo subfolders (each written by _save_run_settings) still record it.
    """
    for entry in _iter_combo_dirs(project_dir):
        try:
            run_settings = json.loads(_read_case_file(str(entry), "run_settings.json"))
        except Exception:
            continue
        guv_path = run_settings.get("guv_path")
        if guv_path:
            return guv_path
    return None


def rebuild_project_status_from_disk(project_dir, project_name, room):
    """Reconstruct a project_status.json from what's actually already on
    disk, for a project directory that predates this feature - real
    completed/incomplete runs exist, they just were never status-tracked.

    Scans every combo subfolder for its own run_settings.json (written by
    _save_run_settings - the exact settings that combo actually ran with,
    including every FLOW_FINGERPRINT_FIELDS value at the time, which is
    what makes recomputing flow_fingerprint here meaningful rather than
    just guessing) and results.json (its presence means "done"; its
    absence means the combo started setup but never finished, recorded as
    "incomplete" - descriptive only, nothing downstream currently branches
    on that exact string). uv_fingerprint is recomputed from the
    subfolder's own 0/kUV if still present.

    Deliberately does NOT try to recover ach_bases (the shared
    _base_ACH*/_control_ACH* scratch dirs) - a project predating this
    feature predates keep-shared-scratch-dirs defaulting True too, so
    those are very unlikely to still be on disk; find_reusable_ach_base's
    own existence check already degrades gracefully to "nothing to reuse"
    without a record, so there's nothing incorrect about leaving it empty.

    Returns the number of combo subfolders successfully read (0 means
    nothing here to rebuild from, not a bug).
    """
    n_found = 0
    for entry in _iter_combo_dirs(project_dir):
        try:
            run_settings = json.loads(_read_case_file(str(entry), "run_settings.json"))
        except Exception:
            continue
        z, ach = run_settings.get("z-value"), run_settings.get("ach")
        if z is None or ach is None:
            continue
        n_found += 1
        try:
            flow_fingerprint = compute_flow_fingerprint(run_settings, room)
        except Exception:
            flow_fingerprint = None
        try:
            uv_fingerprint = compute_uv_fingerprint(str(entry))
        except Exception:
            uv_fingerprint = None
        has_results = Path(f"{entry}/results.json").exists()
        update_combo_status(
            project_dir, project_name, z, ach,
            guv_path=run_settings.get("guv_path"), settings_path=run_settings.get("settings_path"),
            sim_type=run_settings.get("sim-type"),
            status="done" if has_results else "incomplete",
            flow_fingerprint=flow_fingerprint, uv_fingerprint=uv_fingerprint,
        )
    return n_found


def continue_decay(case_dir, end_time, write_interval, log_fn=print, should_stop=None, should_pause=None,
                    solver_log_fn=None):
    """Extend an already-completed decay run to a longer duration, reusing
    the existing mesh/converged flow field/UV zones as-is - only pimpleFoam
    (and the postProcess/results steps after it) reruns.

    Shared by both apps (moved here from app.py's own _continue_decay,
    2026-08-10, once the Qt app's own "Extend / modify simulations" flow
    needed the identical capability - see qtapp/sweep_state.launch_extend)
    - takes log_fn/should_stop/should_pause/solver_log_fn as plain
    parameters rather than closing over either app's own global run state,
    matching every other GUI-independent pipeline function in this module.

    Two controlDict states are needed, not one: startFrom=latestTime makes
    the *solver* resume from whatever time directory is already on disk
    (verified: it genuinely continues the physics, not just relabeling t=0).
    But postProcess -dict system/volAverageDict honors that same setting for
    its own processing range too - left on latestTime, it only recomputes
    the single newest time step rather than the whole curve (verified
    directly: postProcessing/volAverage1/90/ instead of the expected .../0/,
    containing just one row). So startFrom is switched back to startTime
    (endTime stays at the new, higher value) before postProcess runs, so it
    walks the full 0..end_time history and produces one continuous merged
    decay curve - not something that needs manual stitching in Python.

    Returns the freshly-written results dict (same shape write_results_summary
    always returns). Raises RuntimeError if there's no existing results.json
    to extend, or if pimpleFoam itself fails; raises StoppedByUser if
    should_stop() fires mid-solve.
    """
    results_path = f"{case_dir}/results.json"
    if not Path(results_path).exists():
        raise RuntimeError(
            f"No existing results.json in {case_dir} - run a full simulation "
            f"here first before continuing it."
        )
    with open(results_path) as f:
        prior = json.load(f)

    case_dir_wsl = wsl_path(case_dir)
    log_fn(f"Resuming from the latest existing time directory, extending to {end_time}s "
           f"(mesh, flow field, and UV zones are untouched)...")
    set_control_dict_start_from(case_dir, "latestTime")
    set_control_dict_time(case_dir, end_time=end_time, write_interval=write_interval)

    log_fn(f"Running pimpleFoam to {end_time}s...")
    r = run_wsl_streaming(
        "pimpleFoam 2>&1 | tee -a log.pimpleFoam", case_dir_wsl,
        on_line=solver_log_fn, should_stop=should_stop, kill_pattern="pimpleFoam",
        should_pause=should_pause,
    )
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"pimpleFoam failed (exit {r.returncode}):\n{tail}")

    log_fn("Running postProcess volAverage (recomputing the full merged decay curve)...")
    set_control_dict_start_from(case_dir, "startTime")
    run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing stale postProcessing")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    log_fn("Computing spatial coefficient of variation (final concentration field)...")
    try:
        spatial_cov = spatial_coefficient_of_variation(read_latest_time_field(case_dir, "T"))
    except Exception as e:
        log_fn(f"  Could not compute spatial CoV: {e}")
        spatial_cov = None

    log_fn("Writing results summary...")
    extra = {k: prior[k] for k in ("n_lamps", "fluence_mean", "flow_converged", "ach_delivery") if k in prior}
    extra["spatial_cov_final"] = spatial_cov
    results = write_results_summary(
        case_dir, results_path, prior["ventilation_ach"], prior["eACH_uv_well_mixed"],
        # write_results_summary's own DEFAULT vol_average_dat points at the
        # live-volAverage path (postProcessing/volAverageLive1/...) that a
        # normal run's own solve writes as it goes - this function instead
        # ran a POST-HOC `postProcess -dict system/volAverageDict` just
        # above, which writes function object volAverage1's output to
        # postProcessing/volAverage1/0/... (0, not "latest", since
        # startFrom was reset to startTime right before running it) - so
        # this must be pointed there explicitly, or it silently reads a
        # stale/absent live file instead of the curve just recomputed.
        vol_average_dat="postProcessing/volAverage1/0/volFieldValue.dat",
        extra=extra or None,
        # The control run itself isn't redone here (mesh/flow/UV zones are
        # untouched - see this function's own docstring), but its earlier
        # measured ventilation rate is still valid against the extended
        # curve, so re-supply it here rather than silently reverting to
        # the nominal-ACH-only ("uncorrected") fields Continue used to.
        measured_ventilation_ach=prior.get("ventilation_ach_measured"),
        measured_ventilation_ach_ci95=prior.get("ventilation_ach_measured_ci95"),
    )
    log_fn(f"Done. eACH_uv effective={results['eACH_uv_effective']:.4g} /hr "
           f"(well-mixed={results['eACH_uv_well_mixed']:.4g} /hr)")
    return results


def run_decay_sweep(guv_path, settings_path, project_dir, room, settings, adv,
                     z_values, ach_values, log_fn=print, should_stop=None,
                     on_combo_done=None, solver_log_fn=None, status_fn=None, should_pause=None):
    """Decay-mode equivalent of run_sweep() - same Z x ACH cross-product,
    same shared-flow-field-per-ACH reuse (_build_flow_base/_copy_base_case/
    _apply_z are identical either way - only the per-combination solve
    itself differs).

    Every stage (per-ACH flow convergence, control, per-Z decay) is
    submitted to ONE orchestration pool, sized generously (see below) so a
    thread merely blocked waiting on another future never starves real
    work - actual concurrent-solve capacity is capped separately by
    solve_semaphore, to _MAX_CONCURRENT_SOLVES workers (see that
    constant's own docstring for exactly how priority between stages
    falls out of submission order, and this function's own ach_worker
    (inside) for the per-ACH sequencing). Unlike run_sweep's Phase 1/Phase 2
    (a real dependency - Phase 2 clones Phase 1's own converged
    checkpoint), decay mode has no Phase 1 at all: control and every Z's
    own UV-on decay solve are BOTH only ever dependent on the converged
    flow base, never on each other, so they're submitted together the
    moment flow finishes - control_results is only ever consumed for
    post-hoc corrected reporting (a subtraction, not a solver input),
    never as a prerequisite for actually running a Z's own decay solve
    (the one exception - mechanical_ach_only, which reuses control's own
    results.json file verbatim instead of running anything of its own -
    still just waits on control's own future, not a structural rebuild).

    status_fn(key, line_or_None), if given, receives each concurrent
    combo's latest "Time = N" line to display in place (overwritten, not
    appended - see _throttled_solver_callback) instead of the scrolling
    log; called with None once that combo's stream finishes, so the
    caller can drop it from whatever "currently running" display it
    keeps.
    """
    combos = sweep_combinations(z_values, ach_values)
    achs = sorted({ach for _, ach in combos})
    project_name = _sanitize(Path(project_dir).name)
    # Real concurrent-solve capacity is capped by solve_semaphore (acquired
    # only around each actual OpenFOAM invocation - see its own docstring
    # on e.g. run_pipeline.converge_flow_field), NOT by this pool's own
    # worker count. The pool itself is sized generously - one worker per
    # task that could ever be in flight at once (flow-prep + control per
    # ACH, plus every combo) - so a thread merely blocked waiting on
    # another future (e.g. run_z_fn awaiting control_results_future) never
    # starves a genuinely free solve slot elsewhere. Confirmed as a real
    # bug when the pool's own worker count was the only concurrency limit:
    # a Z-combo that finished its own solve faster than its ACH's shared
    # control run sat blocked holding a pool worker for the rest of
    # control's runtime, silently stealing that slot from other ACH
    # groups' work that had nothing left to wait on (2026-08-20).
    max_concurrent_solves = adv.get("max-concurrent-solves", _MAX_CONCURRENT_SOLVES)
    solve_semaphore = threading.Semaphore(max_concurrent_solves)
    pool = ThreadPoolExecutor(max_workers=len(achs) * 2 + len(combos))

    # Each "" for this project's original design/mode (today's exact
    # naming, unchanged) - see compute_guv_design_suffix/
    # compute_sim_type_suffix's own docstrings for the incident this
    # closes: a genuinely different .guv and/or a switched simulation
    # mode applied to Z/ACH values this project already used, without
    # this, would silently land on and get skipped as those SAME
    # already-done combos. Concatenated (guv first, mode second) into one
    # combo_suffix threaded through every combo's own folder/status-key
    # naming below.
    existing_status = load_project_status(project_dir, project_name)
    original_guv_path = existing_status.get("guv_path")
    original_sim_type = existing_status.get("sim_type")
    guv_suffix = compute_guv_design_suffix(guv_path, original_guv_path)
    sim_type_suffix = compute_sim_type_suffix("decay", original_sim_type)
    combo_suffix = guv_suffix + sim_type_suffix

    mechanical_ach_only = bool(settings.get("mech-ach-only"))

    def _prepare_flow(ach):
        sealed = ach <= 0
        if sealed and not settings.get("fan-enable"):
            raise ValueError(
                f"ACH={ach}: a sealed room (ACH<=0) needs the mixing fan enabled - "
                "with no ventilation and no fan, there's no way for the flow field to develop.")
        if mechanical_ach_only and sealed:
            raise ValueError(
                f"ACH={ach}: mechanical-ACH-only needs real ventilation (ACH>0) - a sealed room "
                "has no mechanical ventilation to measure.")
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        base_dir = f"{project_dir}/_base_ACH{_ach_label(ach)}"
        control_dir = f"{project_dir}/_control_ACH{_ach_label(ach)}"
        flow_fingerprint = compute_flow_fingerprint(settings, room)

        # Sealed rooms never share a control run (see ach_worker's own
        # sealed branch) and their flow base is Z/UV-independent already
        # for a different reason - reuse detection here is only
        # meaningful for the ordinary, ventilated case.
        reusable = None if sealed else find_reusable_ach_base(project_dir, project_name, ach, flow_fingerprint)
        if reusable is None:
            _discard_stale_ach_scratch_if_mismatched(project_dir, project_name, ach, flow_fingerprint,
                                                       [base_dir, control_dir], ach_log_fn)
            if not sealed:
                _seed_ach_base_if_no_scratch_survives(project_dir, project_name, ach, flow_fingerprint,
                                                       base_dir, ach_log_fn, "decay", original_sim_type)

        ach_log_fn("=== converging flow field (once per ACH) ===")
        base_summary = _build_flow_base(guv_path, base_dir, room, settings, ach, adv,
                                         ach_log_fn, should_stop, solver_log_fn, should_pause=should_pause,
                                         sealed=sealed, mechanical_ach_only=mechanical_ach_only,
                                         solve_semaphore=solve_semaphore)
        return {"ach": ach, "base_dir": base_dir, "control_dir": control_dir, "sealed": sealed,
                "base_summary": base_summary, "flow_fingerprint": flow_fingerprint, "reusable": reusable,
                "fan_kw": _fan_kwargs(settings)}

    def _prepare_control(flow_ctx):
        ach = flow_ctx["ach"]
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        if flow_ctx["reusable"] is not None:
            ach_log_fn("Reusing this ACH's already-run UV-off control decay (unchanged flow "
                       "settings since an earlier sweep) - skipping a repeat pimpleFoam run.")
            control_results = flow_ctx["reusable"]["control_results"]
        else:
            # UV-off control is Z-independent (see _run_shared_control) -
            # run it once per ACH here, not once per Z in run_z_fn below -
            # and (2026-08-11) concurrently with every Z's own decay solve
            # for this ACH, not before them: neither depends on the other.
            control_results = _run_shared_control(flow_ctx["base_dir"], flow_ctx["control_dir"], ach, room,
                                                   settings, adv, ach_log_fn, should_stop, solver_log_fn,
                                                   flow_ctx["base_summary"], status_fn=status_fn,
                                                   should_pause=should_pause, sealed=False,
                                                   solve_semaphore=solve_semaphore)
        _update_ach_base_status_safe(project_dir, project_name, ach, flow_ctx["flow_fingerprint"],
                                      flow_ctx["base_dir"], flow_ctx["control_dir"], control_results,
                                      guv_path=guv_path, settings_path=settings_path, sim_type="decay")
        return control_results

    def run_z_fn(ctx, z, ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before the next combination.")

        combo_log_fn = _prefixed_log_fn(log_fn, f"Z={z}/ACH={ach}")
        subdir = _subdir_name(z, ach, combo_suffix)
        case_dir = f"{project_dir}/{subdir}"
        combo_log_fn(f"--- -> {subdir} ---")
        if _skip_if_combo_already_done(case_dir, subdir, combo_log_fn, _trim_decay_report, on_combo_done, z, ach):
            return
        flow_fingerprint = compute_flow_fingerprint(settings, room)
        _update_combo_status_safe(project_dir, project_name, z, ach, guv_path=guv_path,
                                   settings_path=settings_path, sim_type="decay", status="running",
                                   started_at=now_iso(), flow_fingerprint=flow_fingerprint,
                                   combo_suffix=combo_suffix, subdir=subdir)
        try:
            _copy_base_case(ctx["base_dir"], case_dir, combo_log_fn)
            if mechanical_ach_only:
                # No UV at all - Z is physically irrelevant (nothing in the
                # case depends on it), and the shared per-ACH control run
                # already IS the measurement this combination wants:
                # re-running _apply_z/_run_decay_scenario here would just be
                # pimpleFoam solving the exact same empty-fvOptions physics
                # a second (or third...) time. Unlike the normal branch
                # below, this DOES need control_results resolved right away
                # (not just at report time) - it reads control's own
                # results.json file directly, which only exists once
                # control's future has actually settled.
                combo_log_fn("  Mechanical ACH only - reusing this ACH's shared control result "
                             "(no UV, so Z has no effect)...")
                ctx["control_results_future"].result()
                control_dir_content = _read_case_file(ctx["control_dir"], "results.json")
                write_case_file(case_dir, "results.json", control_dir_content)
                result = json.loads(control_dir_content)
                uv_fingerprint = None  # no UV/lamp physics involved at all in this mode
            else:
                z_summary = _apply_z(case_dir, z, adv["uv-zone-bins"], ctx["fan_kw"], combo_log_fn,
                                  adaptive_t_relaxation=adv["adaptive-t-relaxation"],
                                  scalar_relaxation=adv["scalar-relaxation"])
                uv_fingerprint = compute_uv_fingerprint(case_dir)
                # The future itself is handed through, not .result() here -
                # control runs concurrently with this Z's own decay solve
                # now (2026-08-11); resolving eagerly at this point would
                # block this Z's own pimpleFoam call from ever starting
                # until control's had already finished, silently
                # serializing the two. _run_decay_scenario resolves it
                # itself, at the one point it's actually needed (see its
                # own docstring) - after, not before, this combo's solve.
                result = _run_decay_scenario(case_dir, room, settings, z, ach, adv,
                                              z_summary, combo_log_fn, should_stop, solver_log_fn,
                                              ctx["control_results_future"], base_summary=ctx["base_summary"],
                                              status_fn=status_fn, should_pause=should_pause,
                                              solve_semaphore=solve_semaphore)
            capture_openfoam_settings(settings, adv)
            _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach)

            trimmed = _trim_decay_report(result)
            report_relative = f"{project_name}_{subdir}_report.json"
            report_path = f"{project_dir}/{report_relative}"
            write_case_file(project_dir, report_relative, json.dumps(trimmed, indent=2))
            combo_log_fn(f"  Done. eACH_uv effective={result['eACH_uv_effective']:.4g} /hr "
                         f"(well-mixed={result['eACH_uv_well_mixed']:.4g} /hr)")
            _update_combo_status_safe(project_dir, project_name, z, ach, status="done",
                                       finished_at=now_iso(), uv_fingerprint=uv_fingerprint,
                                       combo_suffix=combo_suffix, subdir=subdir)
            if on_combo_done:
                on_combo_done(z, ach, "done", trimmed)
        except StoppedByUser:
            _update_combo_status_safe(project_dir, project_name, z, ach, status="stopped", finished_at=now_iso(),
                                       combo_suffix=combo_suffix, subdir=subdir)
            raise
        except Exception as e:
            combo_log_fn(f"ERROR: {e}")
            _update_combo_status_safe(project_dir, project_name, z, ach, status="error",
                                       error_message=str(e), finished_at=now_iso(),
                                       combo_suffix=combo_suffix, subdir=subdir)
            if on_combo_done:
                on_combo_done(z, ach, "error", str(e))

    def cleanup_ach_fn(ctx):
        ach = ctx["ach"]
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        if adv.get("keep-shared-scratch-dirs"):
            ach_log_fn("Keeping shared base case and control run on disk "
                       "(\"Keep shared per-ACH scratch directories\" is enabled)...")
            return
        ach_log_fn("Removing shared base case and control run...")
        run_wsl_or_raise(
            f'rm -rf "{wsl_path(ctx["base_dir"])}" "{wsl_path(ctx["control_dir"])}"', wsl_path(project_dir),
            "cleaning up shared base case and control run")

    def ach_worker(ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before starting the next ACH group.")
        flow_ctx = pool.submit(_prepare_flow, ach).result()
        if flow_ctx["sealed"]:
            # No possible path for contaminant MASS to leave a sealed room
            # (a fan redistributes air but can't remove contaminant from a
            # closed room) - the true ventilation-only decay rate is
            # exactly 0 by construction, not just approximately - see
            # app._finish_decay's identical reasoning. Running a whole 2nd
            # pimpleFoam solve to confirm that would double this ACH
            # group's compute cost for no new information, so this is a
            # value already in hand, not a real background task - see
            # _completed_future's own docstring for why run_z_fn still
            # gets the exact same "control_results_future" shape either way.
            _prefixed_log_fn(log_fn, f"ACH={ach}")(
                "Skipping the UV-off control run - sealed room, ventilation-only decay "
                "rate is exactly 0 by construction.")
            control_future = _completed_future({"total_ach_effective": 0.0})
        else:
            control_future = pool.submit(_prepare_control, flow_ctx)
        zs = [z for z, a in combos if a == ach]
        z_ctx = dict(flow_ctx, control_results_future=control_future)
        z_futures = [pool.submit(run_z_fn, z_ctx, z, ach) for z in zs]
        try:
            for f in z_futures:
                f.result()  # re-raises StoppedByUser; per-combo errors are already caught inside run_z_fn
        finally:
            # Always wait for control to physically finish before cleanup
            # runs, even when unwinding from a Z failure above - cleanup
            # deletes control_dir, and doing that while control is still
            # actively writing to it would be a real race (rm -rf against
            # an in-progress OpenFOAM case), not just a theoretical one.
            # futures.wait() never raises, unlike .result() - control's own
            # exception (if any) is deliberately re-raised separately
            # below, so a genuine control failure still surfaces as this
            # ACH group's overall failure, same as it always has, without
            # masking whatever already failed above.
            futures_wait([control_future])
            cleanup_ach_fn(flow_ctx)
        control_future.result()

    try:
        _run_ach_pool(achs, ach_worker, should_stop)
    finally:
        pool.shutdown(wait=True)
        write_sweep_summary_csv(project_dir, project_name)


def _trim_report(result):
    """A copy of a steady-state results dict with the bulky per-iteration
    arrays stripped - phase1/phase2's "live"/"decay_curve", and each
    monitoring point's own per-iteration series - everything else
    (reduction_pct, eACH_uv figures, ACHeff, target_T_ss, ...) untouched.
    """
    trimmed = dict(result)
    for phase_key in ("phase1", "phase2"):
        phase = trimmed.get(phase_key)
        if phase:
            phase = dict(phase)
            phase.pop("live", None)
            phase.pop("decay_curve", None)
            trimmed[phase_key] = phase
    monitoring = trimmed.get("monitoring")
    if monitoring:
        trimmed_monitoring = {}
        for name, point in monitoring.items():
            trimmed_point = {}
            for phase_key, phase_data in point.items():
                phase_data = dict(phase_data)
                phase_data.pop("t_seconds", None)
                phase_data.pop("volAverage_T", None)
                trimmed_point[phase_key] = phase_data
            trimmed_monitoring[name] = trimmed_point
        trimmed["monitoring"] = trimmed_monitoring
    return trimmed


def run_sweep(guv_path, settings_path, project_dir, room, settings, adv,
              z_values, ach_values, log_fn=print, should_stop=None,
              on_combo_done=None, solver_log_fn=None, status_fn=None, should_pause=None):
    """Run the full Z x ACH cross-product against an already-loaded
    project, one subfolder per combination directly under project_dir,
    reusing a single converged flow field, a single converged Phase 1
    ("source only, no UV") run, AND a single UV-off control run for every
    Z sharing an ACH (see module docstring - _run_shared_phase1,
    _run_shared_control). The control run measures the actual ventilation
    rate directly (same method decay mode already uses), which is more
    reliable than deriving it from Phase 1's own point-source buildup -
    see compute_corrected_eACH_uv_from_control's docstring. Writes
    results.json/run_settings.json into each subfolder (same as a normal
    single run - "Export Report" and ParaView both work per-subfolder
    unchanged) plus a trimmed, compound-named report.json directly in
    project_dir.

    on_combo_done(z, ach, status, detail), if given, is called after each
    combination - status is "done"/"error"; detail is the trimmed result
    dict on success or the exception message on failure. Used by the GUI
    to update a live progress table.

    A failed combination is logged and skipped - the sweep continues to
    the next one. should_stop() is checked before each ACH group and each
    combination (raises StoppedByUser to abort the rest of the sweep, same
    pattern every other pipeline entry point already uses) - not currently
    checked *within* a combination's own setup_case()/
    run_steady_state_scenario() call beyond what those functions already
    do internally.

    Every stage (per-ACH flow convergence, Phase 1, control, per-Z Phase 2)
    is submitted to ONE orchestration pool, sized generously (see below)
    so a thread merely blocked waiting on another future never starves
    real work - actual concurrent-solve capacity is capped separately by
    solve_semaphore, to _MAX_CONCURRENT_SOLVES workers (see that
    constant's own docstring for exactly how priority between stages
    falls out of submission order, and this function's own ach_worker
    (inside) for the per-ACH sequencing): flow first; then
    Phase 1 and control together (genuine siblings - Phase 2 needs Phase
    1's converged checkpoint specifically, so that wait is a real
    dependency, not just a priority preference; control needs only the
    converged flow base, so it runs alongside Phase 1 rather than after
    it); then, once Phase 1 resolves, every Z sharing this ACH.

    status_fn(key, line_or_None), if given, receives each concurrent
    combo's latest "Time = N" line to display in place instead of the
    scrolling log - see run_decay_sweep's own docstring for the same
    mechanism.
    """
    if any(ach <= 0 for ach in ach_values):
        raise ValueError("Sealed-room / ACH<=0 is only supported in Decay mode - "
                          "steady-state has no sensible zero-ventilation case.")

    combos = sweep_combinations(z_values, ach_values)
    achs = sorted({ach for _, ach in combos})
    project_name = _sanitize(Path(project_dir).name)
    # See run_decay_sweep's identical comment - real concurrent-solve
    # capacity is capped by solve_semaphore, not by this pool's own worker
    # count, which is instead sized generously (flow-prep + Phase 1 +
    # control per ACH, plus every combo) purely for orchestration/waiting
    # headroom.
    max_concurrent_solves = adv.get("max-concurrent-solves", _MAX_CONCURRENT_SOLVES)
    solve_semaphore = threading.Semaphore(max_concurrent_solves)
    pool = ThreadPoolExecutor(max_workers=len(achs) * 3 + len(combos))

    # Each "" for this project's original design/mode (today's exact
    # naming, unchanged) - see compute_guv_design_suffix/
    # compute_sim_type_suffix's own docstrings for the incident this
    # closes: a genuinely different .guv and/or a switched simulation
    # mode applied to Z/ACH values this project already used, without
    # this, would silently land on and get skipped as those SAME
    # already-done combos. Concatenated (guv first, mode second) into one
    # combo_suffix threaded through every combo's own folder/status-key
    # naming below. In practice run_sweep's own sim_type is always
    # "steady_state" - only run_decay_sweep's own mode-switch direction
    # (steady_state -> decay) is offered in either app's UI - but this is
    # computed symmetrically here too rather than hardcoded to "", so a
    # hypothetical reverse switch is equally protected against colliding
    # with existing combos, not just the one direction the UI exposes.
    existing_status = load_project_status(project_dir, project_name)
    original_guv_path = existing_status.get("guv_path")
    original_sim_type = existing_status.get("sim_type")
    guv_suffix = compute_guv_design_suffix(guv_path, original_guv_path)
    sim_type_suffix = compute_sim_type_suffix("steady_state", original_sim_type)
    combo_suffix = guv_suffix + sim_type_suffix

    def _prepare_flow(ach):
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        base_dir = f"{project_dir}/_base_ACH{_fmt(ach)}"
        phase1_dir = f"{project_dir}/_phase1_ACH{_fmt(ach)}"
        control_dir = f"{project_dir}/_control_ACH{_fmt(ach)}"
        flow_fingerprint = compute_flow_fingerprint(settings, room)

        reusable = find_reusable_ach_base(project_dir, project_name, ach, flow_fingerprint)
        if reusable is None:
            _discard_stale_ach_scratch_if_mismatched(project_dir, project_name, ach, flow_fingerprint,
                                                       [base_dir, phase1_dir, control_dir], ach_log_fn)
            _seed_ach_base_if_no_scratch_survives(project_dir, project_name, ach, flow_fingerprint,
                                                   base_dir, ach_log_fn, "steady_state", original_sim_type)

        ach_log_fn("=== converging flow field (once per ACH) ===")
        base_summary = _build_flow_base(guv_path, base_dir, room, settings, ach, adv, ach_log_fn, should_stop,
                                         solver_log_fn, should_pause=should_pause, solve_semaphore=solve_semaphore)
        return {"ach": ach, "base_dir": base_dir, "phase1_dir": phase1_dir, "control_dir": control_dir,
                "base_summary": base_summary, "flow_fingerprint": flow_fingerprint, "reusable": reusable,
                "fan_kw": _fan_kwargs(settings)}

    def _prepare_phase1(flow_ctx):
        ach = flow_ctx["ach"]
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        # Phase 1 ("source only, no UV") is Z-independent (see
        # _run_shared_phase1) - run it once per ACH here, not once per Z
        # in run_z_fn below. Its own reuse check (checkpoint/pending on
        # disk) isn't fingerprint-aware either, but _prepare_flow's own
        # stale-discard already removed phase1_dir if it belonged to a
        # mismatched fingerprint, so whatever's left here (if anything)
        # is safe.
        _run_shared_phase1(flow_ctx["base_dir"], flow_ctx["phase1_dir"], ach, room, settings, adv, ach_log_fn,
                            should_stop, solver_log_fn, status_fn=status_fn, should_pause=should_pause,
                            solve_semaphore=solve_semaphore)

    def _prepare_control(flow_ctx):
        ach = flow_ctx["ach"]
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        if flow_ctx["reusable"] is not None:
            ach_log_fn("Reusing this ACH's already-run UV-off control decay (unchanged flow "
                       "settings since an earlier sweep) - skipping a repeat pimpleFoam run.")
            control_results = flow_ctx["reusable"]["control_results"]
        else:
            # A dedicated UV-off control run (same one decay mode already uses)
            # measures the actual ventilation rate directly, without Phase 1's
            # point-source mixing-transport lag - see
            # compute_corrected_eACH_uv_from_control's docstring for why that
            # matters. Shared per ACH, same as the flow base and Phase 1 - and
            # (2026-08-11) runs CONCURRENTLY with Phase 1, not after it, since
            # it only needs the converged flow base, not Phase 1's own result.
            control_results = _run_shared_control(flow_ctx["base_dir"], flow_ctx["control_dir"], ach, room,
                                                   settings, adv, ach_log_fn, should_stop, solver_log_fn,
                                                   flow_ctx["base_summary"], status_fn=status_fn,
                                                   should_pause=should_pause, solve_semaphore=solve_semaphore)
        _update_ach_base_status_safe(project_dir, project_name, ach, flow_ctx["flow_fingerprint"],
                                      flow_ctx["base_dir"], flow_ctx["control_dir"], control_results,
                                      guv_path=guv_path, settings_path=settings_path, sim_type="steady_state")
        return control_results

    def run_z_fn(ctx, z, ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before the next combination.")

        combo_log_fn = _prefixed_log_fn(log_fn, f"Z={z}/ACH={ach}")
        subdir = _subdir_name(z, ach, combo_suffix)
        case_dir = f"{project_dir}/{subdir}"
        combo_log_fn(f"--- -> {subdir} ---")
        if _skip_if_combo_already_done(case_dir, subdir, combo_log_fn, _trim_report, on_combo_done, z, ach):
            return
        flow_fingerprint = compute_flow_fingerprint(settings, room)
        _update_combo_status_safe(project_dir, project_name, z, ach, guv_path=guv_path,
                                   settings_path=settings_path, sim_type="steady_state", status="running",
                                   started_at=now_iso(), flow_fingerprint=flow_fingerprint,
                                   combo_suffix=combo_suffix, subdir=subdir)
        try:
            _copy_base_case(ctx["phase1_dir"], case_dir, combo_log_fn)
            z_summary = _apply_z(case_dir, z, adv["uv-zone-bins"], ctx["fan_kw"], combo_log_fn,
                                  adaptive_t_relaxation=adv["adaptive-t-relaxation"],
                                  scalar_relaxation=adv["scalar-relaxation"])
            uv_fingerprint = compute_uv_fingerprint(case_dir)
            # _apply_z's write_cellzones() rewrites cellZones from scratch,
            # wiping the source cellZone _run_shared_phase1 already carved
            # into phase1_dir (the same reason _apply_z itself re-carves
            # the fan zone) - re-carve it here too so Phase 2's source
            # fvOptions entry still resolves against a real cellZone.
            write_source_topo_set_dict(
                case_dir, (settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
                resolve_source_size(settings, adv["mesh-cell-size"], (room.x, room.y, room.z)),
                cell_size=adv["mesh-cell-size"], room_dims=(room.x, room.y, room.z))
            run_wsl_or_raise("topoSet -dict system/sourceTopoSetDict", wsl_path(case_dir),
                              "topoSet (restoring source zone wiped by _apply_z)")
            # The future itself is handed through, not .result() here -
            # control runs concurrently with Phase 1/Phase 2 now
            # (2026-08-11); resolving eagerly at this point would block
            # Phase 2's own simpleFoam call from ever starting until
            # control had already finished, silently serializing the two.
            # _run_scenario/run_steady_state_scenario resolve it themselves,
            # at the one point it's actually needed (see their own
            # docstrings) - after, not before, this combo's own solve.
            result = _run_scenario(case_dir, room, settings, z, ach, adv,
                                    z_summary, combo_log_fn, should_stop, solver_log_fn,
                                    status_fn=status_fn, control_results_future=ctx["control_results_future"],
                                    base_summary=ctx["base_summary"], should_pause=should_pause,
                                    solve_semaphore=solve_semaphore)
            try:
                snapshot_openfoam_settings(case_dir)
            except Exception:
                pass  # archival only - never block a results.json write over it
            write_case_file(case_dir, "results.json", json.dumps(result, indent=2))
            capture_openfoam_settings(settings, adv)
            _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach)

            trimmed = _trim_report(result)
            report_relative = f"{project_name}_{subdir}_report.json"
            report_path = f"{project_dir}/{report_relative}"
            write_case_file(project_dir, report_relative, json.dumps(trimmed, indent=2))
            combo_log_fn(f"  Done. Reduction={result['reduction_pct']:.1f}%, "
                         f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")
            _update_combo_status_safe(project_dir, project_name, z, ach, status="done",
                                       finished_at=now_iso(), uv_fingerprint=uv_fingerprint,
                                       combo_suffix=combo_suffix, subdir=subdir)
            if on_combo_done:
                on_combo_done(z, ach, "done", trimmed)
        except StoppedByUser:
            _update_combo_status_safe(project_dir, project_name, z, ach, status="stopped", finished_at=now_iso(),
                                       combo_suffix=combo_suffix, subdir=subdir)
            raise
        except Exception as e:
            combo_log_fn(f"ERROR: {e}")
            _update_combo_status_safe(project_dir, project_name, z, ach, status="error",
                                       error_message=str(e), finished_at=now_iso(),
                                       combo_suffix=combo_suffix, subdir=subdir)
            if on_combo_done:
                on_combo_done(z, ach, "error", str(e))

    def cleanup_ach_fn(ctx):
        ach = ctx["ach"]
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        if adv.get("keep-shared-scratch-dirs"):
            ach_log_fn("Keeping shared base case, Phase 1 run, and control run on disk "
                       "(\"Keep shared per-ACH scratch directories\" is enabled)...")
            return
        ach_log_fn("Removing shared base case, Phase 1 run, and control run...")
        run_wsl_or_raise(
            f'rm -rf "{wsl_path(ctx["base_dir"])}" "{wsl_path(ctx["phase1_dir"])}" "{wsl_path(ctx["control_dir"])}"',
            wsl_path(project_dir), "cleaning up shared base case, Phase 1 run, and control run")

    def ach_worker(ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before starting the next ACH group.")
        flow_ctx = pool.submit(_prepare_flow, ach).result()
        # Phase 1 and control are genuine siblings (both only need the
        # converged flow base) - submitted together, Phase 1 textually
        # first so it wins a tie if only one pool slot is free at this
        # exact instant, matching the stated priority without needing a
        # real priority queue.
        phase1_future = pool.submit(_prepare_phase1, flow_ctx)
        control_future = pool.submit(_prepare_control, flow_ctx)
        try:
            phase1_future.result()  # Phase 2 clones phase1_dir - a real dependency, not just a preference
            zs = [z for z, a in combos if a == ach]
            z_ctx = dict(flow_ctx, control_results_future=control_future)
            z_futures = [pool.submit(run_z_fn, z_ctx, z, ach) for z in zs]
            for f in z_futures:
                f.result()  # re-raises StoppedByUser; per-combo errors are already caught inside run_z_fn
        finally:
            # Always wait for control to physically finish before cleanup
            # runs, even when unwinding from a Phase 1/Z failure above -
            # cleanup deletes control_dir, and doing that while control is
            # still actively writing to it would be a real race (rm -rf
            # against an in-progress OpenFOAM case), not just a
            # theoretical one. futures.wait() never raises, unlike
            # .result() - control's own exception (if any) is deliberately
            # re-raised separately below, so a genuine control failure
            # still surfaces as this ACH group's overall failure, same as
            # it always has, without masking whatever already failed above.
            futures_wait([control_future])
            cleanup_ach_fn(flow_ctx)
        control_future.result()

    try:
        _run_ach_pool(achs, ach_worker, should_stop)
    finally:
        pool.shutdown(wait=True)
        # Written even on StoppedByUser/a partial sweep - a summary of
        # whatever combinations did finish is more useful than none.
        write_sweep_summary_csv(project_dir, project_name)
