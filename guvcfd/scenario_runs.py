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
from pathlib import Path

import numpy as np

from .case_io import read_boundary_patch_names, read_openfoam_scalar_field, write_scalar_field
from .cellzones import bin_decay_rates, write_cellzones
from .fan import fan_fvoptions_entry, write_fan_topo_set_dict
from .fluence import compute_inactivation_rate, compute_well_mixed_eACH
from .initial_fields import compute_inlet_velocities
from .run_pipeline import setup_case
from .steady_state_pipeline import run_steady_state_scenario
from .visualization import center_frac_for_wall
from .wsl_utils import StoppedByUser, run_wsl_or_raise, wsl_path

_TEMPLATE_CASE_DIR = str(Path(__file__).resolve().parent / "templates" / "case_template")

_UNSAFE_FOLDER_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


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
