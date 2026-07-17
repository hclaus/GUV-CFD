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
    write_fvoptions_file,
)
from .decay_analysis import read_vol_average_dat, check_plateau, windowed_stats
from .initial_fields import restore_boundary_conditions, resolve_inlet_velocity
from .mesh_gen import opening_center
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


def _copy_latest_to_zero(case_dir_wsl, latest, include_T, log_fn):
    fields = "U p k omega nut phi" + (" T" if include_T else "")
    r = run_wsl_or_raise(f"ls {latest}/", case_dir_wsl, "listing converged fields")
    available = set(r.stdout.split())
    to_copy = [f for f in fields.split() if f in available]
    log_fn(f"  Copying fields from {latest}/ to 0/: {to_copy}")
    cp_targets = " ".join(f"{latest}/{f}" for f in to_copy)
    run_wsl_or_raise(f"cp -f {cp_targets} 0/", case_dir_wsl, "copying converged fields")


def _run_phase(case_dir, case_dir_wsl, n_iterations, write_interval, plateau_window, plateau_rel_tol,
                log_fn, should_stop=None, solver_log_fn=None, live_monitoring_zones=(),
                live_patches=()):
    set_control_dict_time(case_dir, end_time=n_iterations, write_interval=write_interval, delta_t=1)
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
    else:
        # The set_control_dict_time() call above sweeps writeInterval
        # everywhere in the file, including inside these live blocks
        # (left over from an earlier phase) - re-pin them to 1 without
        # touching the main solve's own writeInterval.
        for name in live_block_names:
            set_function_write_interval(case_dir, name, 1)

    log_fn(f"Running simpleFoam ({n_iterations} iterations, writing every {write_interval})...")
    r = run_wsl_streaming(
        "simpleFoam 2>&1 | tee log.simpleFoam", case_dir_wsl,
        on_line=solver_log_fn or log_fn, should_stop=should_stop, kill_pattern="simpleFoam",
    )
    if should_stop is not None and should_stop():
        raise StoppedByUser("Stopped during simpleFoam phase.")
    if r.returncode != 0 or "FOAM FATAL" in r.stdout or "Floating Point Exception" in r.stdout:
        tail = "\n".join(r.stdout.splitlines()[-25:]) or "(no output captured)"
        raise RuntimeError(f"simpleFoam failed (exit {r.returncode}):\n{tail}")

    r = run_wsl_or_raise(
        "ls -d [0-9]*/ 2>/dev/null | sed 's#/##' | sort -n | tail -1",
        case_dir_wsl, "listing time directories",
    )
    latest = r.stdout.strip()
    if not latest or latest == "0":
        raise RuntimeError(f"simpleFoam did not write any new time directory (found: {latest!r})")

    # The live function object already wrote its own
    # postProcessing/*Live/<startTime>/ files during the solve above - read
    # those back *before* wiping postProcessing/ for the postProcess pass,
    # since postProcess doesn't touch (or need) them.
    live_curves = {"room": read_vol_average_dat(
        f"{case_dir}/postProcessing/volAverageLive1/0/volFieldValue.dat")}
    for zone in live_monitoring_zones:
        live_curves[zone] = read_vol_average_dat(
            f"{case_dir}/postProcessing/monitor_{zone}Live/0/volFieldValue.dat")

    run_wsl_or_raise("rm -rf postProcessing", case_dir_wsl, "clearing stale postProcessing")
    run_wsl_or_raise("postProcess -dict system/volAverageDict", case_dir_wsl, "postProcess volAverage")
    t, T = read_vol_average_dat(f"{case_dir}/postProcessing/volAverage1/0/volFieldValue.dat")
    converged, rel_spread = check_plateau(T, window=plateau_window, rel_tol=plateau_rel_tol)
    log_fn(f"  Stopped at time {latest}. T_ss={T[-1]:.4g} (last-{plateau_window} rel. spread={rel_spread:.4g}, "
           f"{'plateaued' if converged else 'NOT YET PLATEAUED - consider more iterations'})")
    return latest, t, T, converged, live_curves


def _room_phase_summary(live_room, window_frac, converged, iterations, sparse_t, sparse_T, log_fn):
    """Room-wide phase1/phase2 entry: T_ss is now the trailing-window mean
    of the live per-iteration series (not the single last sample) - see
    windowed_stats. `decay_curve`/`live` (sparse postProcess read / dense
    per-iteration read) are both kept as-is for result_figures.py.
    """
    live_t, live_T = live_room
    mean, std, cv, n, span = windowed_stats(live_t, live_T, frac=window_frac)
    cv_text = f"{cv * 100:.1f}%" if cv is not None else "n/a"
    log_fn(f"  Moving average (last {span:.4g} iterations, n={n}): {mean:.4g} (CV={cv_text})")
    return {
        "T_ss": mean, "T_ss_std": std, "T_ss_cv": cv, "T_ss_window_span": span,
        "T_ss_window_n": n, "T_ss_window_frac": window_frac,
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
    mean, std, cv, n, span = windowed_stats(t, T, frac=window_frac)
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
                               plateau_window=5, plateau_rel_tol=0.01, window_frac=0.15,
                               fan_entry=None, monitoring_points=None,
                               patches_to_monitor=("outlet",), log_fn=print, should_stop=None,
                               solver_log_fn=None):
    """Run both phases of a continuous-source steady-state scenario against
    an already-converged case (mesh + flow + fluenceRate/kUV must already
    exist - see run_pipeline.setup_case()). Returns a summary dict.

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
    write_source_topo_set_dict(case_dir, source_center, source_size)
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
        center = opening_center(inlet_wall, room_x, room_y, room_z, inlet_center, inlet_size)
        inlet_velocity = resolve_inlet_velocity(case_dir, "inlet", inlet_wall, center, v_mag, "ceiling")
    if inlet2_diffuser_type == "ceiling" and inlet2_velocity is not None:
        v_mag2 = float(np.linalg.norm(inlet2_velocity))
        center2 = opening_center(inlet2_wall, room_x, room_y, room_z, inlet2_center, inlet2_size)
        inlet2_velocity = resolve_inlet_velocity(case_dir, "inlet2", inlet2_wall, center2, v_mag2, "ceiling")

    # --- Phase 1: source only, no UV ---
    log_fn("=== Phase 1: source only (no UV) ===")
    write_fvoptions_file(case_dir, [source_entry] + fan_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch: {n_open} vs {n_close}"
    restore_boundary_conditions(case_dir, inlet_velocity=inlet_velocity, T_initial=0,
                                 inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2)

    latest1, t1, T1, converged1, live1 = _run_phase(
        case_dir, case_dir_wsl, phase1_iterations, phase1_write_interval,
        plateau_window, plateau_rel_tol, log_fn, should_stop=should_stop,
        solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
        live_patches=patches_to_monitor,
    )
    summary["phase1"] = _room_phase_summary(live1["room"], window_frac, converged1, latest1, t1, T1, log_fn)
    _copy_latest_to_zero(case_dir_wsl, latest1, include_T=True, log_fn=log_fn)
    _clean_time_dirs(case_dir_wsl)

    # --- Phase 2: source + UV ---
    log_fn("=== Phase 2: source + UV ===")
    k_values = read_openfoam_scalar_field(f"{case_dir}/0/kUV")
    uv_entries = _uv_fvoptions_entries(np.array(k_values), nbins)
    write_fvoptions_file(case_dir, [source_entry] + uv_entries + fan_entries)
    _, n_open, n_close = splice_fv_options_into_control_dict(case_dir)
    assert n_open == n_close, f"Brace mismatch: {n_open} vs {n_close}"

    latest2, t2, T2, converged2, live2 = _run_phase(
        case_dir, case_dir_wsl, phase2_iterations, phase2_write_interval,
        plateau_window, plateau_rel_tol, log_fn, should_stop=should_stop,
        solver_log_fn=solver_log_fn, live_monitoring_zones=live_zone_names,
        live_patches=patches_to_monitor,
    )
    summary["phase2"] = _room_phase_summary(live2["room"], window_frac, converged2, latest2, t2, T2, log_fn)
    if monitoring_points:
        summary["monitoring"] = {
            p["name"]: {
                "phase1": _point_phase_summary(live1[zone_name(p["name"])], window_frac),
                "phase2": _point_phase_summary(live2[zone_name(p["name"])], window_frac),
            }
            for p in monitoring_points
        }
    _copy_latest_to_zero(case_dir_wsl, latest2, include_T=True, log_fn=log_fn)

    lambda_vent = ach / 3600.0
    T_ss1, T_ss2 = summary["phase1"]["T_ss"], summary["phase2"]["T_ss"]
    reduction_pct = (1 - T_ss2 / T_ss1) * 100 if T_ss1 else None
    eACH_uv = lambda_vent * (T_ss1 / T_ss2 - 1) * 3600 if T_ss2 else None
    summary["reduction_pct"] = reduction_pct
    summary["eACH_uv_steady_state"] = eACH_uv
    log_fn(f"Reduction: {reduction_pct:.1f}%, eACH_uv (steady-state method) = {eACH_uv:.4g} /hr")

    # Corrected eACH_uv using the actual (not nominal) ventilation removal
    # rate - see compute_corrected_eACH_uv's docstring. Unlike the decay
    # scenario, this is free: no separate UV-off control run needed.
    ventilation_ach_measured, eACH_uv_corrected = compute_corrected_eACH_uv(
        T_ss1, T_ss2, Su, source_volume, room_volume)
    if ventilation_ach_measured is not None:
        summary["ventilation_ach_measured"] = ventilation_ach_measured
        summary["eACH_uv_steady_state_corrected"] = eACH_uv_corrected
        log_fn(f"  Measured ventilation ACH (from Phase 1's own steady state) = "
               f"{ventilation_ach_measured:.4g} /hr (nominal was {ach:.4g} /hr); "
               f"corrected eACH_uv = {eACH_uv_corrected:.4g} /hr")

    run_wsl_or_raise("touch case.foam", case_dir_wsl, "touching case.foam")

    log_fn("Steady-state scenario complete.")
    return summary
