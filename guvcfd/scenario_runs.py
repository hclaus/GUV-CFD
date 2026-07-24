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
import threading
from pathlib import Path

import numpy as np

from .case_io import read_boundary_patch_names, read_openfoam_scalar_field, write_scalar_field
from .cellzones import bin_decay_rates, write_cellzones
from .decay_analysis import write_results_summary
from .fan import fan_fvoptions_entry, write_fan_topo_set_dict
from .fluence import compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import compute_inlet_velocities
from .monitoring_points import compute_monitoring_results
from .run_pipeline import setup_case
from .splice import set_control_dict_time
from .steady_state_pipeline import run_steady_state_scenario
from .ventilation_control import prepare_ventilation_only_control, finish_ventilation_only_control
from .visualization import center_frac_for_wall
from .wsl_utils import StoppedByUser, run_wsl_or_raise, run_wsl_streaming, wsl_path

_TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "templates" / "case_template")

_UNSAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Matches pimpleFoam's per-timestep "Time = N" banner, not the residual/
# Courant-number/continuity-error lines that follow it - see
# _run_decay_pair, which throttles concurrent decay runs' log_fn output to
# this instead of flooding with the full per-iteration dump for both runs.
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

def _build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, should_stop, solver_log_fn):
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
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn,
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


def _run_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn):
    """run_steady_state_scenario() with this combination's z/ach - same
    call app._run_steady_state makes for a single run.
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
    phase1_iterations = max(settings["phase1-iterations"], _settling_iterations(ach))
    phase2_iterations = max(settings["phase2-iterations"], _settling_iterations(ach + eACH_uv))

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
        window_frac=settings.get("t-ss-window-frac") or 0.15,
        cell_size=adv["mesh-cell-size"], nbins=adv["uv-zone-bins"],
        source_size=adv["source-zone-size"],
        plateau_rel_tol=adv["plateau-rel-tol"] / 100.0,
        t_inf_check_interval=500 if adv["t-infinity-early-stop-enabled"] else None,
        t_inf_rel_tol=(adv["t-infinity-rel-tol"] / 100.0) if adv["t-infinity-early-stop-enabled"] else None,
        keep_all_timesteps=adv["keep-all-timesteps"],
        fan_entry=fan_entry, monitoring_points=_gather_monitoring_points(settings),
        patches_to_monitor=patches_to_monitor,
        log_fn=log_fn, should_stop=should_stop, solver_log_fn=solver_log_fn,
    )
    result["fluence_mean"] = z_summary["fluence_mean"]
    result["eACH_uv_well_mixed"] = z_summary.get("eACH_uv_well_mixed_mean")
    return result


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


def _run_decay_pair(case_dir_wsl, control_dir_wsl, should_stop, log_fn, solver_log_fn):
    """Run the UV-on and UV-off-control pimpleFoam solves concurrently -
    see app._run_decay_pair (same design, just parametrized instead of
    using the Dash app's global _run_log/_should_stop/_track_solver_time).
    """
    results = {}
    errors = {}

    def run_one(name, cwd_wsl, on_line, log_prefix):
        try:
            def prefixed(line):
                stripped = line.strip()
                # "[...]"-wrapped lines are run_wsl_streaming's own
                # diagnostics (stall/retry notices), not solver chatter -
                # always shown, never throttled like routine "Time = N"
                # lines, so a stall/kill is never silent in the log.
                if _TIME_LINE_RE.match(stripped) or stripped.startswith("["):
                    log_fn(f"[{log_prefix}] {line}")
                if on_line:
                    on_line(line)
            results[name] = run_wsl_streaming(
                "pimpleFoam 2>&1 | tee log.pimpleFoam", cwd_wsl,
                on_line=prefixed, should_stop=should_stop, kill_pattern="pimpleFoam",
            )
        except Exception as e:
            errors[name] = e

    th_uv = threading.Thread(target=run_one, args=("uv", case_dir_wsl, solver_log_fn, "UV-on"))
    th_control = threading.Thread(target=run_one, args=("control", control_dir_wsl, None, "control"))
    th_uv.start()
    th_control.start()
    th_uv.join()
    th_control.join()

    if errors:
        raise next(iter(errors.values()))
    return results["uv"], results["control"]


def _run_decay_scenario(case_dir, room, settings, z, ach, adv, z_summary, log_fn, should_stop, solver_log_fn,
                         base_summary=None):
    """Decay-mode equivalent of _run_scenario() - runs the UV-on decay and
    its UV-off control concurrently (always both - see app._finish_decay)
    at this combination's z/ach, with adaptive durations, then writes the
    same corrected results.json shape a single decay run would.

    Unlike steady-state's _run_scenario, this doesn't need
    run_steady_state_scenario at all - setup_case()/_apply_z() (called by
    the caller before this) already carved this Z's UV cellZones/fvOptions
    into case_dir; a decay run's own scalar-transport function object and
    fvOptions splice were already done inside setup_case() too, so this
    only needs to (re)set the adaptive duration and run the two solves.
    """
    case_dir_wsl = wsl_path(case_dir)
    control_dir = f"{case_dir}/no_UV"
    control_dir_wsl = wsl_path(control_dir)

    eACH_well_mixed_est = z_summary.get("eACH_uv_well_mixed_mean", 0.0)
    combined_end_time, control_end_time = _decay_run_durations(ach, eACH_well_mixed_est, adv)
    log_fn(f"  Adaptive run durations: UV-on={combined_end_time}s, UV-off control={control_end_time}s...")
    set_control_dict_time(case_dir, end_time=combined_end_time,
                           write_interval=max(1, combined_end_time // 100), delta_t=adv["pimple-delta-t"])

    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped before pimpleFoam.")
    has_inlet2 = bool(settings.get("inlet2-enable"))
    log_fn("  Preparing UV-off control (subfolder \"no_UV\")...")
    prepare_ventilation_only_control(
        case_dir, control_dir, ach, room.x, room.y, room.z,
        settings["inlet-wall"], (settings["inlet-size-w"], settings["inlet-size-h"]),
        control_end_time, max(1, control_end_time // 100), pimple_delta_t=adv["pimple-delta-t"],
        inlet2_wall=settings["inlet2-wall"] if has_inlet2 else None,
        inlet2_size=(settings["inlet2-size-w"], settings["inlet2-size-h"]) if has_inlet2 else None,
        has_outlet2=bool(settings.get("outlet2-enable")),
        log_fn=log_fn, should_stop=should_stop,
    )

    log_fn(f"  Running pimpleFoam concurrently: UV-on ({combined_end_time}s) + "
           f"UV-off control ({control_end_time}s)...")
    r_uv, r_control = _run_decay_pair(case_dir_wsl, control_dir_wsl, should_stop, log_fn, solver_log_fn)
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped during pimpleFoam.")
    for label, r in (("UV-on", r_uv), ("UV-off control", r_control)):
        if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
            tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
            raise RuntimeError(f"{label} pimpleFoam failed (exit {r.returncode}):\n{tail}")

    log_fn("  Running postProcess volAverage...")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")

    log_fn("  Post-processing UV-off control...")
    control_results = finish_ventilation_only_control(control_dir, ach, log_fn=log_fn)

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


def run_decay_sweep(guv_path, settings_path, project_dir, room, settings, adv,
                     z_values, ach_values, log_fn=print, should_stop=None,
                     on_combo_done=None, solver_log_fn=None):
    """Decay-mode equivalent of run_sweep() - same Z x ACH cross-product,
    same shared-flow-field-per-ACH reuse (_build_flow_base/_copy_base_case/
    _apply_z are identical either way - only the per-combination solve
    itself differs), but running the UV-on decay and its UV-off control
    concurrently for each combination instead of steady_state_pipeline's
    two-phase build-up (see _run_decay_scenario).
    """
    combos = sweep_combinations(z_values, ach_values)
    achs = sorted({ach for _, ach in combos})
    project_name = _sanitize(Path(project_dir).name)

    for ach in achs:
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before starting the next ACH group.")

        base_dir = f"{project_dir}/_base_ACH{_fmt(ach)}"
        log_fn(f"=== ACH={ach}: converging flow field (shared by every Z at this ACH) ===")
        base_summary = _build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, should_stop, solver_log_fn)
        fan_kw = _fan_kwargs(settings)

        try:
            for z, combo_ach in combos:
                if combo_ach != ach:
                    continue
                if should_stop is not None and should_stop():
                    raise StoppedByUser("Stopped before the next combination.")

                subdir = _subdir_name(z, ach)
                case_dir = f"{project_dir}/{subdir}"
                log_fn(f"--- Z={z}, ACH={ach} -> {subdir} ---")
                try:
                    _copy_base_case(base_dir, case_dir, log_fn)
                    z_summary = _apply_z(case_dir, z, adv["uv-zone-bins"], fan_kw, log_fn)
                    result = _run_decay_scenario(case_dir, room, settings, z, ach, adv,
                                                  z_summary, log_fn, should_stop, solver_log_fn,
                                                  base_summary=base_summary)
                    _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach)

                    trimmed = _trim_decay_report(result)
                    report_path = f"{project_dir}/{project_name}_{subdir}_report.json"
                    with open(report_path, "w") as f:
                        json.dump(trimmed, f, indent=2)
                    log_fn(f"  Done. eACH_uv effective={result['eACH_uv_effective']:.4g} /hr "
                           f"(well-mixed={result['eACH_uv_well_mixed']:.4g} /hr)")
                    if on_combo_done:
                        on_combo_done(z, ach, "done", trimmed)
                except StoppedByUser:
                    raise
                except Exception as e:
                    log_fn(f"ERROR (Z={z}, ACH={ach}): {e}")
                    if on_combo_done:
                        on_combo_done(z, ach, "error", str(e))
        finally:
            log_fn(f"  Removing shared base case for ACH={ach}...")
            run_wsl_or_raise(f'rm -rf "{wsl_path(base_dir)}"', wsl_path(project_dir),
                              "cleaning up shared base case")


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
              on_combo_done=None, solver_log_fn=None):
    """Run the full Z x ACH cross-product against an already-loaded
    project, one subfolder per combination directly under project_dir,
    reusing a single converged flow field for every Z sharing an ACH (see
    module docstring). Writes results.json/run_settings.json into each
    subfolder (same as a normal single run - "Export Report" and
    ParaView both work per-subfolder unchanged) plus a trimmed, compound-
    named report.json directly in project_dir.

    on_combo_done(z, ach, status, detail), if given, is called after each
    combination - status is "done"/"error"; detail is the trimmed result
    dict on success or the exception message on failure. Used by the GUI
    to update a live progress table.

    A failed combination is logged and skipped - the sweep continues to
    the next one. should_stop() is checked between combinations (raises
    StoppedByUser to abort the rest of the sweep, same pattern every
    other pipeline entry point already uses) - not currently checked
    *within* a combination's own setup_case()/run_steady_state_scenario()
    call beyond what those functions already do internally.
    """
    combos = sweep_combinations(z_values, ach_values)
    achs = sorted({ach for _, ach in combos})
    project_name = _sanitize(Path(project_dir).name)

    for ach in achs:
        if should_stop is not None and should_stop():
            raise StoppedByUser("Stopped before starting the next ACH group.")

        base_dir = f"{project_dir}/_base_ACH{_fmt(ach)}"
        log_fn(f"=== ACH={ach}: converging flow field (shared by every Z at this ACH) ===")
        _build_flow_base(guv_path, base_dir, room, settings, ach, adv, log_fn, should_stop, solver_log_fn)
        fan_kw = _fan_kwargs(settings)

        try:
            for z, combo_ach in combos:
                if combo_ach != ach:
                    continue
                if should_stop is not None and should_stop():
                    raise StoppedByUser("Stopped before the next combination.")

                subdir = _subdir_name(z, ach)
                case_dir = f"{project_dir}/{subdir}"
                log_fn(f"--- Z={z}, ACH={ach} -> {subdir} ---")
                try:
                    _copy_base_case(base_dir, case_dir, log_fn)
                    z_summary = _apply_z(case_dir, z, adv["uv-zone-bins"], fan_kw, log_fn)
                    result = _run_scenario(case_dir, room, settings, z, ach, adv,
                                            z_summary, log_fn, should_stop, solver_log_fn)
                    with open(f"{case_dir}/results.json", "w") as f:
                        json.dump(result, f, indent=2)
                    _save_run_settings(case_dir, settings, guv_path, settings_path, z, ach)

                    trimmed = _trim_report(result)
                    report_path = f"{project_dir}/{project_name}_{subdir}_report.json"
                    with open(report_path, "w") as f:
                        json.dump(trimmed, f, indent=2)
                    log_fn(f"  Done. Reduction={result['reduction_pct']:.1f}%, "
                           f"eACH_uv={result['eACH_uv_steady_state']:.4g} /hr")
                    if on_combo_done:
                        on_combo_done(z, ach, "done", trimmed)
                except StoppedByUser:
                    raise
                except Exception as e:
                    log_fn(f"ERROR (Z={z}, ACH={ach}): {e}")
                    if on_combo_done:
                        on_combo_done(z, ach, "error", str(e))
        finally:
            log_fn(f"  Removing shared base case for ACH={ach}...")
            run_wsl_or_raise(f'rm -rf "{wsl_path(base_dir)}"', wsl_path(project_dir),
                              "cleaning up shared base case")
