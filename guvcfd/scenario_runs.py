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
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from .case_io import read_boundary_patch_names, read_openfoam_scalar_field, write_scalar_field
from .cellzones import bin_decay_rates, write_cellzones
from .contaminant_source import write_fvoptions_file, write_source_topo_set_dict
from .decay_analysis import write_results_summary
from .fan import fan_fvoptions_entry, write_fan_topo_set_dict
from .fluence import compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import compute_inlet_velocities
from .monitoring_points import compute_monitoring_results
from .run_pipeline import setup_case
from .splice import set_control_dict_time, splice_fv_options_into_control_dict
from .steady_state_pipeline import run_steady_state_scenario, _uv_fvoptions_entries, resolve_phase_delta_ts
from .ventilation_control import prepare_ventilation_only_control, finish_ventilation_only_control
from .visualization import center_frac_for_wall
from .wsl_utils import StoppedByUser, run_wsl_or_raise, run_wsl_streaming, wsl_path

_TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "templates" / "case_template")

# Two-level concurrency budget for run_sweep/run_decay_sweep (see
# _run_sweep_concurrent): up to this many ACH groups build their flow field
# at once, and - across whichever ACH groups are active - up to this many
# Z values' own solves run at once, drawn from ONE shared pool rather than
# one pool per ACH (so total concurrent OpenFOAM processes stays bounded by
# MAX_ACH + MAX_Z regardless of how many ACH/Z values are actually swept).
# _MAX_CONCURRENT_Z is the one that matters day-to-day - once an ACH
# group's one-time flow convergence finishes, its slot in the ACH pool
# goes idle and everything left running draws from the Z pool alone (a
# sweep with 1-2 ACH values spends most of its time this way) - confirmed
# on a real sweep: only 3 cores stayed busy during the per-Z decay-solving
# phase even though up to 6 (MAX_ACH + MAX_Z) was the assumed target.
_MAX_CONCURRENT_ACH = 3
_MAX_CONCURRENT_Z = 6

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


def _subdir_name(z, ach):
    return _sanitize(f"Z{_fmt(z)}_ACH{_fmt(ach)}")


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
    with open(f"{case_dir}/run_settings.json", "w") as f:
        json.dump(data, f, indent=2)


# --- flow-field build/reuse ---

def _build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, should_stop, solver_log_fn,
                      should_pause=None):
    """setup_case() into base_dir at this ACH - the project's currently
    configured Z is used as a placeholder (every Z-dependent file this
    writes gets overwritten by _apply_z before any subfolder actually
    runs), exactly the same call app._run_steady_state makes for a single
    run, just targeting a temp directory.
    """
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
        scalar_transport_ncorr=adv["scalar-transport-ncorr"],
        scalar_transport_tolerance=adv["scalar-transport-tolerance"],
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn, should_pause=should_pause,
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


def _apply_z(case_dir, Z, nbins, fan_kwargs, log_fn):
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
    """
    patch_names = read_boundary_patch_names(case_dir)
    fluence_values = np.array(read_openfoam_scalar_field(f"{case_dir}/0/fluenceRate"))
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

    return {
        "fluence_mean": float(fluence_values.mean()),
        "eACH_uv_well_mixed_mean": float(eACH_values.mean()),
    }


def _run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                   status_fn=None, control_results=None, base_summary=None, should_pause=None):
    """run_steady_state_scenario() with this combination's z/ach - same
    call app._run_steady_state makes for a single run.

    status_fn, if given, receives each phase's latest "Time = N" line to
    display in place instead of the scrolling log - see
    steady_state_pipeline.run_steady_state_scenario's own docstring.

    control_results: the shared, once-per-ACH UV-off control's own result
    dict (see _run_shared_control) - its measured ventilation rate feeds
    run_steady_state_scenario's measured_ventilation_ach, which is more
    reliable than deriving one from Phase 1's own point-source buildup
    (see compute_corrected_eACH_uv_from_control's docstring). None falls
    back to the old Phase-1-derived method.

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
    openings = [(settings["inlet-wall"], settings["inlet-size-w"] * settings["inlet-size-h"])]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    if has_inlet2:
        openings.append((settings["inlet2-wall"], settings["inlet2-size-w"] * settings["inlet2-size-h"]))
    velocities = compute_inlet_velocities(ach, room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None
    has_outlet2 = bool(settings.get("outlet2-enable"))

    eACH_uv = z_summary.get("eACH_uv_well_mixed_mean", 0.0)
    if adv["deltat-scaling-enabled"]:
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
    phase1_delta_t, phase2_delta_t = resolve_phase_delta_ts(ach, eACH_uv, phase1_iterations, phase2_iterations, adv)

    patches_to_monitor = ("outlet", "outlet2") if has_outlet2 else ("outlet",)
    result = run_steady_state_scenario(
        case_dir, room.x, room.y, room.z, ach, z,
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=settings["target-t-ss"],
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
        source_size=settings["source-zone-size"],
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        t_inf_check_interval=adv["phase-chunk-size"] if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        keep_all_timesteps=adv["keep-all-timesteps"],
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn, should_pause=should_pause,
        status_fn=status_fn,
        measured_ventilation_ach=(control_results or {}).get("total_ach_effective"),
        phase1_delta_t=phase1_delta_t, phase2_delta_t=phase2_delta_t,
    )
    result["fluence_mean"] = z_summary["fluence_mean"]
    result["eACH_uv_well_mixed"] = z_summary.get("eACH_uv_well_mixed_mean")
    result["flow_converged"] = (base_summary or {}).get("flow_converged")
    result["ach_delivery"] = (base_summary or {}).get("ach_delivery")
    result["n_lamps"] = (base_summary or {}).get("n_lamps")
    return result


def _run_shared_phase1(base_dir, phase1_dir, ach, room, settings, adv, log_fn, should_stop, solver_log_fn,
                        status_fn=None, should_pause=None):
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
    """
    _copy_base_case(base_dir, phase1_dir, log_fn)

    fan_entry = None
    if settings.get("fan-enable"):
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_entry = fan_fvoptions_entry(settings["fan-speed"], direction=direction)

    room_volume = room.x * room.y * room.z
    openings = [(settings["inlet-wall"], settings["inlet-size-w"] * settings["inlet-size-h"])]
    has_inlet2 = bool(settings.get("inlet2-enable"))
    if has_inlet2:
        openings.append((settings["inlet2-wall"], settings["inlet2-size-w"] * settings["inlet2-size-h"]))
    velocities = compute_inlet_velocities(ach, room_volume, openings)
    inlet_velocity = velocities[0]
    inlet2_velocity = velocities[1] if has_inlet2 else None
    has_outlet2 = bool(settings.get("outlet2-enable"))

    if adv["deltat-scaling-enabled"]:
        phase1_iterations = settings["phase1-iterations"]
    else:
        phase1_iterations = max(settings["phase1-iterations"], _settling_iterations(ach))
    # Phase 1 alone has no UV/Z dependency, so eACH_uv_well_mixed=0 here -
    # phase2_delta_t is discarded (phase1_only=True below runs no Phase 2).
    phase1_delta_t, _ = resolve_phase_delta_ts(ach, 0.0, phase1_iterations, phase1_iterations, adv)
    patches_to_monitor = ("outlet", "outlet2") if has_outlet2 else ("outlet",)

    log_fn(f"=== ACH={ach}: Phase 1 (source only, no UV - shared by every Z at this ACH) ===")
    run_steady_state_scenario(
        # Z is a placeholder here (Phase 1 has no UV, so its value is
        # irrelevant) - same convention _build_flow_base's own docstring
        # already uses for the shared flow base.
        phase1_dir, room.x, room.y, room.z, ach, settings["z-value"],
        source_center=(settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
        target_T_ss=settings["target-t-ss"],
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
        source_size=settings["source-zone-size"],
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        t_inf_check_interval=adv["phase-chunk-size"] if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        keep_all_timesteps=adv["keep-all-timesteps"],
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn, should_pause=should_pause,
        status_fn=status_fn, phase1_only=True, phase1_delta_t=phase1_delta_t,
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
    groups/Z values can be solving concurrently (see _run_sweep_concurrent)
    and their log lines interleave; under the old strictly-sequential
    sweep this context was implicit (only one combination's lines were
    ever being produced at a time).
    """
    return lambda msg: log_fn(f"[{prefix}] {msg}")


def _throttled_solver_callback(log_fn, log_prefix, on_line=None, status_fn=None, status_key=None):
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

    The status_fn branch deliberately does NOT prefix the line with
    log_prefix - status_key already carries that (and the combo's Z/ACH
    identity too), and the caller renders "[{key}] {value}" itself (see
    app._poll_scenario) - prefixing here too would just double it up.
    """
    def callback(line):
        stripped = line.strip()
        if _TIME_LINE_RE.match(stripped):
            if status_fn is not None:
                status_fn(status_key, stripped)
            else:
                log_fn(f"[{log_prefix}] {line}")
        elif stripped.startswith("["):
            log_fn(f"[{log_prefix}] {line}")
        if on_line:
            on_line(line)
    return callback


def _run_shared_control(base_dir, control_dir, ach, room, settings, adv, log_fn, should_stop, solver_log_fn,
                         status_fn=None, should_pause=None):
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
    """
    control_dir_wsl = wsl_path(control_dir)
    _, control_end_time = _decay_run_durations(ach, 0.0, adv)
    write_interval = max(1, settings["pimple-write-interval"])
    has_inlet2 = bool(settings.get("inlet2-enable"))

    log_fn(f"=== ACH={ach}: preparing shared UV-off control ({control_end_time}s, "
           f"once for every Z at this ACH) ===")
    prepare_ventilation_only_control(
        base_dir, control_dir, ach, room.x, room.y, room.z,
        settings["inlet-wall"], (settings["inlet-size-w"], settings["inlet-size-h"]),
        control_end_time, write_interval, pimple_delta_t=adv["pimple-delta-t"],
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
        has_outlet2=bool(settings.get("outlet2-enable")),
        log_fn=log_fn, should_stop=should_stop,
    )

    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    log_fn(f"  Running pimpleFoam (shared control, {control_end_time}s)...")
    status_key = f"ACH={ach}/control"
    try:
        r = run_wsl_streaming(
            "pimpleFoam 2>&1 | tee log.pimpleFoam", control_dir_wsl,
            on_line=_throttled_solver_callback(log_fn, "control", status_fn=status_fn, status_key=status_key),
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
                         control_results, base_summary=None, status_fn=None, should_pause=None):
    """Decay-mode equivalent of _run_scenario() - runs this combination's
    own UV-on decay (adaptive duration) and writes the same corrected
    results.json shape a single decay run would, using control_results
    (the shared, once-per-ACH UV-off control - see _run_shared_control)
    instead of running its own redundant copy.

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
    """
    case_dir_wsl = wsl_path(case_dir)

    has_fan = bool(settings.get("fan-enable"))
    fan_entries = []
    if has_fan:
        direction = (0, 0, -1) if settings["fan-direction"] == "down" else (0, 0, 1)
        fan_entries = [fan_fvoptions_entry(settings["fan-speed"], direction=direction)]
    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    uv_entries = _uv_fvoptions_entries(np.array(k_values), adv["uv-zone-bins"])
    write_fvoptions_file(case_dir, uv_entries + fan_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch after UV fvOptions splice: open={n_open} close={n_close}"

    eACH_well_mixed_est = z_summary.get("eACH_uv_well_mixed_mean", 0.0)
    combined_end_time, _ = _decay_run_durations(ach, eACH_well_mixed_est, adv)
    write_interval = max(1, settings["pimple-write-interval"])
    log_fn(f"  Adaptive duration: UV-on={combined_end_time}s, write interval={write_interval}s "
           f"(as configured) - UV-off control is shared for this ACH, not re-run here...")
    set_control_dict_time(case_dir, end_time=combined_end_time,
                           write_interval=write_interval, delta_t=adv["pimple-delta-t"])

    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    log_fn(f"  Running pimpleFoam (UV-on, {combined_end_time}s)...")
    status_key = f"Z={z}/ACH={ach}/UV-on"
    try:
        r_uv = run_wsl_streaming(
            "pimpleFoam 2>&1 | tee log.pimpleFoam", case_dir_wsl,
            on_line=_throttled_solver_callback(log_fn, "UV-on", solver_log_fn,
                                                status_fn=status_fn, status_key=status_key),
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

    log_fn("  Running postProcess volAverage...")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    log_fn("  Writing results summary...")
    result = write_results_summary(
        case_dir, f"{case_dir}/results.json", ach, eACH_well_mixed_est,
        extra={
            "fluence_mean": z_summary.get("fluence_mean"),
            "flow_converged": (base_summary or {}).get("flow_converged"),
            "ach_delivery": (base_summary or {}).get("ach_delivery"),
            "n_lamps": (base_summary or {}).get("n_lamps"),
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
        with open(f"{case_dir}/results.json", "w") as f:
            json.dump(result, f, indent=2)
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


def _run_sweep_concurrent(achs, combos, should_stop, build_ach_fn, run_z_fn, cleanup_ach_fn):
    """Bounded two-level concurrency shared by run_sweep/run_decay_sweep:
    up to _MAX_CONCURRENT_ACH ACH groups build their flow state at once;
    every Z across every active ACH group draws from ONE shared pool of
    _MAX_CONCURRENT_Z workers (not one pool per ACH), so total concurrent
    OpenFOAM solves is bounded by MAX_ACH + MAX_Z regardless of how many
    ACH/Z values are actually swept.

    build_ach_fn(ach) -> ctx, called once per ACH on the ACH pool.
    run_z_fn(ctx, z, ach), called once per Z on the shared Z pool - must
        itself catch and report per-combo errors (StoppedByUser excepted)
        so one Z's failure doesn't cancel its siblings, same as the old
        sequential loops' inline try/except did.
    cleanup_ach_fn(ctx), called once per ACH after every Z under it
        (whether it succeeded, failed, or was skipped) has finished -
        always called if build_ach_fn returned, even on error/stop.
    """
    z_pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_Z)

    def ach_worker(ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before starting the next ACH group.")
        ctx = build_ach_fn(ach)
        zs = [z for z, a in combos if a == ach]
        z_futures = [z_pool.submit(run_z_fn, ctx, z, ach) for z in zs]
        try:
            for f in z_futures:
                f.result()  # re-raises StoppedByUser; per-combo errors are already caught inside run_z_fn
        finally:
            cleanup_ach_fn(ctx)

    try:
        with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_ACH) as ach_pool:
            ach_futures = [ach_pool.submit(ach_worker, ach) for ach in achs]
            for f in as_completed(ach_futures):
                f.result()
    finally:
        z_pool.shutdown(wait=True)


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
    result_path = f"{case_dir}/results.json"
    if not Path(result_path).exists():
        return False
    try:
        with open(result_path) as f:
            result = json.load(f)
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


def run_decay_sweep(guv_path, settings_path, project_dir, room, settings, adv,
                     z_values, ach_values, log_fn=print, should_stop=None,
                     on_combo_done=None, solver_log_fn=None, status_fn=None, should_pause=None):
    """Decay-mode equivalent of run_sweep() - same Z x ACH cross-product,
    same shared-flow-field-per-ACH reuse (_build_flow_base/_copy_base_case/
    _apply_z are identical either way - only the per-combination solve
    itself differs). Up to _MAX_CONCURRENT_ACH ACH groups (each building
    its own flow field + shared UV-off control) run at once, and every
    Z's own UV-on decay draws from one shared pool of _MAX_CONCURRENT_Z
    workers across all of them - see _run_sweep_concurrent.

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

    def build_ach_fn(ach):
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        base_dir = f"{project_dir}/_base_ACH{_fmt(ach)}"
        control_dir = f"{project_dir}/_control_ACH{_fmt(ach)}"
        ach_log_fn("=== converging flow field (shared by every Z at this ACH) ===")
        base_summary = _build_flow_base(guv_path, base_dir, room, settings, ach, adv,
                                         ach_log_fn, should_stop, solver_log_fn, should_pause=should_pause)
        # UV-off control is Z-independent (see _run_shared_control) - run
        # it once per ACH here, not once per Z in run_z_fn below.
        control_results = _run_shared_control(base_dir, control_dir, ach, room, settings, adv,
                                               ach_log_fn, should_stop, solver_log_fn, status_fn=status_fn,
                                               should_pause=should_pause)
        return {
            "ach": ach, "base_dir": base_dir, "control_dir": control_dir,
            "base_summary": base_summary, "control_results": control_results,
            "fan_kw": _fan_kwargs(settings),
        }

    def run_z_fn(ctx, z, ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before the next combination.")

        combo_log_fn = _prefixed_log_fn(log_fn, f"Z={z}/ACH={ach}")
        subdir = _subdir_name(z, ach)
        case_dir = f"{project_dir}/{subdir}"
        combo_log_fn(f"--- -> {subdir} ---")
        if _skip_if_combo_already_done(case_dir, subdir, combo_log_fn, _trim_decay_report, on_combo_done, z, ach):
            return
        try:
            _copy_base_case(ctx["base_dir"], case_dir, combo_log_fn)
            z_summary = _apply_z(case_dir, z, adv["uv-zone-bins"], ctx["fan_kw"], combo_log_fn)
            result = _run_decay_scenario(case_dir, room, settings, z, ach, adv,
                                          z_summary, combo_log_fn, should_stop, solver_log_fn,
                                          ctx["control_results"], base_summary=ctx["base_summary"],
                                          status_fn=status_fn, should_pause=should_pause)
            _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach)

            trimmed = _trim_decay_report(result)
            report_path = f"{project_dir}/{project_name}_{subdir}_report.json"
            with open(report_path, "w") as f:
                json.dump(trimmed, f, indent=2)
            combo_log_fn(f"  Done. eACH_uv effective={result['eACH_uv_effective']:.4g} /hr "
                         f"(well-mixed={result['eACH_uv_well_mixed']:.4g} /hr)")
            if on_combo_done:
                on_combo_done(z, ach, "done", trimmed)
        except StoppedByUser:
            raise
        except Exception as e:
            combo_log_fn(f"ERROR: {e}")
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

    _run_sweep_concurrent(achs, combos, should_stop, build_ach_fn, run_z_fn, cleanup_ach_fn)


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

    Up to _MAX_CONCURRENT_ACH ACH groups (each building its own flow
    field) run at once, and every Z's own steady-state scenario draws from
    one shared pool of _MAX_CONCURRENT_Z workers across all of them - see
    _run_sweep_concurrent.

    status_fn(key, line_or_None), if given, receives each concurrent
    combo's latest "Time = N" line to display in place instead of the
    scrolling log - see run_decay_sweep's own docstring for the same
    mechanism.
    """
    combos = sweep_combinations(z_values, ach_values)
    achs = sorted({ach for _, ach in combos})
    project_name = _sanitize(Path(project_dir).name)

    def build_ach_fn(ach):
        ach_log_fn = _prefixed_log_fn(log_fn, f"ACH={ach}")
        base_dir = f"{project_dir}/_base_ACH{_fmt(ach)}"
        phase1_dir = f"{project_dir}/_phase1_ACH{_fmt(ach)}"
        control_dir = f"{project_dir}/_control_ACH{_fmt(ach)}"
        ach_log_fn("=== converging flow field (shared by every Z at this ACH) ===")
        base_summary = _build_flow_base(guv_path, base_dir, room, settings, ach, adv, ach_log_fn, should_stop,
                                         solver_log_fn, should_pause=should_pause)
        # Phase 1 ("source only, no UV") is Z-independent (see
        # _run_shared_phase1) - run it once per ACH here, not once per Z
        # in run_z_fn below.
        _run_shared_phase1(base_dir, phase1_dir, ach, room, settings, adv, ach_log_fn, should_stop, solver_log_fn,
                            status_fn=status_fn, should_pause=should_pause)
        # A dedicated UV-off control run (same one decay mode already uses)
        # measures the actual ventilation rate directly, without Phase 1's
        # point-source mixing-transport lag - see
        # compute_corrected_eACH_uv_from_control's docstring for why that
        # matters. Shared per ACH, same as the flow base and Phase 1.
        control_results = _run_shared_control(base_dir, control_dir, ach, room, settings, adv,
                                               ach_log_fn, should_stop, solver_log_fn, status_fn=status_fn,
                                               should_pause=should_pause)
        return {"ach": ach, "base_dir": base_dir, "phase1_dir": phase1_dir, "control_dir": control_dir,
                "base_summary": base_summary, "control_results": control_results, "fan_kw": _fan_kwargs(settings)}

    def run_z_fn(ctx, z, ach):
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before the next combination.")

        combo_log_fn = _prefixed_log_fn(log_fn, f"Z={z}/ACH={ach}")
        subdir = _subdir_name(z, ach)
        case_dir = f"{project_dir}/{subdir}"
        combo_log_fn(f"--- -> {subdir} ---")
        if _skip_if_combo_already_done(case_dir, subdir, combo_log_fn, _trim_report, on_combo_done, z, ach):
            return
        try:
            _copy_base_case(ctx["phase1_dir"], case_dir, combo_log_fn)
            z_summary = _apply_z(case_dir, z, adv["uv-zone-bins"], ctx["fan_kw"], combo_log_fn)
            # _apply_z's write_cellzones() rewrites cellZones from scratch,
            # wiping the source cellZone _run_shared_phase1 already carved
            # into phase1_dir (the same reason _apply_z itself re-carves
            # the fan zone) - re-carve it here too so Phase 2's source
            # fvOptions entry still resolves against a real cellZone.
            write_source_topo_set_dict(
                case_dir, (settings["inject-x-input"], settings["inject-y-input"], settings["inject-z-input"]),
                settings["source-zone-size"], cell_size=adv["mesh-cell-size"])
            run_wsl_or_raise("topoSet -dict system/sourceTopoSetDict", wsl_path(case_dir),
                              "topoSet (restoring source zone wiped by _apply_z)")
            result = _run_scenario(case_dir, room, settings, z, ach, adv,
                                    z_summary, combo_log_fn, should_stop, solver_log_fn,
                                    status_fn=status_fn, control_results=ctx["control_results"],
                                    base_summary=ctx["base_summary"], should_pause=should_pause)
            with open(f"{case_dir}/results.json", "w") as f:
                json.dump(result, f, indent=2)
            _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach)

            trimmed = _trim_report(result)
            report_path = f"{project_dir}/{project_name}_{subdir}_report.json"
            with open(report_path, "w") as f:
                json.dump(trimmed, f, indent=2)
            combo_log_fn(f"  Done. Reduction={result['reduction_pct']:.1f}%, "
                         f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")
            if on_combo_done:
                on_combo_done(z, ach, "done", trimmed)
        except StoppedByUser:
            raise
        except Exception as e:
            combo_log_fn(f"ERROR: {e}")
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

    _run_sweep_concurrent(achs, combos, should_stop, build_ach_fn, run_z_fn, cleanup_ach_fn)
