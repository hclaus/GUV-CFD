"""Optional monitoring locations: small box-averaged regions at user-chosen
points in the room (e.g. a patient position, the exhaust), reported
alongside the room-average results.

Purely a post-processing pass over field data that's already been solved
and written to every time directory by the main simulation - no extra CFD
run needed, just an (near-instant) topoSet box-carve and a postProcess pass
that replays already-on-disk data, the same trick used elsewhere in this
package (converge_flow_field's convergence check, the UV-off ventilation
control run, "Continue").
"""
import re

from .decay_analysis import read_vol_average_dat, compute_effective_eACH, windowed_stats
from .wsl_utils import wsl_path, run_wsl_or_raise, write_case_file as _write_case_file

_UNSAFE_ZONE_CHARS_RE = re.compile(r"[^A-Za-z0-9_]+")


def zone_name(label):
    """A monitoring point's display name, sanitized into a valid OpenFOAM
    word token (cellZone/functionObject names can't have spaces or most
    punctuation, and conventionally shouldn't start with a digit)."""
    name = _UNSAFE_ZONE_CHARS_RE.sub("_", label).strip("_")
    if not name:
        return "monitor"
    if name[0].isdigit():
        name = "pt_" + name
    return name


def monitoring_topo_set_dict(points, cell_size):
    """topoSetDict carving one small box cellZone per monitoring point, all
    in a single dict (topoSet applies every action in one invocation).
    Box side length = cells_per_side * cell_size - an honest cell count on
    this package's uniform-cell mesh, not just an approximate physical size.

    points: list of dicts with keys name, x, y, z, cells_per_side.

    A monitoring point's (x, y, z) is an arbitrary user-picked position, not
    generally a multiple of cell_size, so the raw box edges (center +/-
    size/2) can land right on a boxToCell floating-point boundary tie -
    same problem as mesh_gen._opening_box, and the same fix: snap each of
    the 6 edges independently to the nearest grid line, rather than
    snapping the center then assuming a fixed size - snapping the center
    alone would be wrong whenever cells_per_side is odd (an odd cell count
    can't be centered exactly on a mesh vertex, same parity issue as an
    opening), so each edge must be free to land on whichever grid line is
    nearest to it.
    """
    action_lines = []
    for p in points:
        zname = zone_name(p["name"])
        cellset_name = f"{zname}Cells"
        size = p["cells_per_side"] * cell_size
        cx, cy, cz = p["x"], p["y"], p["z"]
        lo = [cx - size / 2, cy - size / 2, cz - size / 2]
        hi = [cx + size / 2, cy + size / 2, cz + size / 2]
        for i in range(3):
            lo[i] = round(lo[i] / cell_size) * cell_size
            hi[i] = round(hi[i] / cell_size) * cell_size
            if hi[i] <= lo[i]:
                hi[i] = lo[i] + cell_size
        action_lines += [
            "    {", f"        name    {cellset_name};", "        type    cellSet;",
            "        action  new;", "        source  boxToCell;",
            f"        box     ({lo[0]:.6g} {lo[1]:.6g} {lo[2]:.6g}) "
            f"({hi[0]:.6g} {hi[1]:.6g} {hi[2]:.6g});",
            "    }",
            "    {", f"        name    {zname};", "        type    cellZoneSet;",
            "        action  new;", "        source  setToCellZone;",
            f"        set     {cellset_name};", "    }",
        ]
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      topoSetDict;", "}", "",
        "actions", "(",
        *action_lines,
        ");", "",
    ]
    return "\n".join(lines)


def write_monitoring_topo_set_dict(case_dir, points, cell_size,
                                    filename="monitoringTopoSetDict"):
    path = f"{case_dir}/system/{filename}"
    _write_case_file(case_dir, f"system/{filename}", monitoring_topo_set_dict(points, cell_size))
    return path


def monitoring_average_dict(points, field="T"):
    """One volFieldValue function object per monitoring point, each
    restricted to that point's own box cellZone (regionType cellZone)
    instead of the whole room - otherwise the same volAverage pattern as
    monitoring.vol_average_dict(). All points share a single readFields
    entry for the field, rather than reloading it once per point.
    """
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      monitoringAverageDict;", "}", "",
        "functions", "{",
        f"    read{field}", "    {",
        "        type            readFields;",
        '        libs            ("libfieldFunctionObjects.so");',
        f"        fields          ({field});",
        "        executeControl  timeStep;",
        "        executeInterval 1;",
        "    }", "",
    ]
    for p in points:
        zname = zone_name(p["name"])
        lines += [
            f"    monitor_{zname}", "    {",
            "        type            volFieldValue;",
            '        libs            ("libfieldFunctionObjects.so");',
            f"        fields          ({field});",
            "        operation       volAverage;",
            "        regionType      cellZone;",
            f"        name            {zname};",
            "        executeControl  timeStep;",
            "        executeInterval 1;",
            "        writeControl    timeStep;",
            "        writeInterval   1;",
            "        writeFields     false;",
            "    }", "",
        ]
    lines += ["}", ""]
    return "\n".join(lines)


def write_monitoring_average_dict(case_dir, points, field="T",
                                   filename="monitoringAverageDict"):
    path = f"{case_dir}/system/{filename}"
    _write_case_file(case_dir, f"system/{filename}", monitoring_average_dict(points, field))
    return path


def compute_monitoring_results(case_dir, points, cell_size=0.1,
                                ventilation_ach=None, fit_decay=True, log_fn=print):
    """Carve each enabled point's box cellZone and read back its
    volAverage(T) curve from the time-directory data already on disk (the
    main simulation must already have run). Returns
    {name: {"t_seconds": [...], "volAverage_T": [...],
            "eACH_uv_effective": float}} - the eACH_uv_effective key is only
    present when fit_decay=True and ventilation_ach is given (meaningful for
    a decay-mode curve; not for a steady-state build-up curve, which isn't a
    decay and would fit garbage).

    Reads directly from whatever time directories currently exist in
    case_dir - call this before any cleanup step (e.g. steady-state's
    _clean_time_dirs) removes the ones you want covered.
    """
    if not points:
        return {}
    case_dir_wsl = wsl_path(case_dir)

    log_fn(f"Carving {len(points)} monitoring zone(s): "
           f"{', '.join(p['name'] for p in points)}...")
    write_monitoring_topo_set_dict(case_dir, points, cell_size)
    run_wsl_or_raise("topoSet -dict system/monitoringTopoSetDict", case_dir_wsl,
                      "topoSet (monitoring zones)")

    write_monitoring_average_dict(case_dir, points)
    run_wsl_or_raise("postProcess -dict system/monitoringAverageDict", case_dir_wsl,
                      "postProcess (monitoring locations)")

    results = {}
    for p in points:
        zname = zone_name(p["name"])
        dat_path = f"{case_dir}/postProcessing/monitor_{zname}/0/volFieldValue.dat"
        t, T = read_vol_average_dat(dat_path)
        entry = {"t_seconds": t.tolist(), "volAverage_T": T.tolist()}
        if fit_decay and ventilation_ach is not None and len(t) > 2:
            fit = compute_effective_eACH(t, T, ventilation_ach)
            entry["eACH_uv_effective"] = fit["eACH_uv_effective"]
            entry["eACH_uv_effective_ci95"] = fit["ci95_eACH_per_hr"]
        results[p["name"]] = entry
        suffix = f", eACH_uv={entry['eACH_uv_effective']:.4g}/hr" if "eACH_uv_effective" in entry else ""
        log_fn(f"  {p['name']}: {len(t)} points, final volAverage(T)={T[-1]:.4g}{suffix}")
    return results


# How far a monitoring point's final T can sit from the room-average final T
# before it's worth calling out - room-average volAverage(T) is a spatial
# average, and real rooms are rarely well mixed; a plain average number can
# badly misrepresent the concentration at any one occupied location.
_MIXING_UNIFORMITY_THRESHOLD = 0.15


def point_reduction_basis(p1, p2):
    """(T1, T2, reduction_pct, basis) for one monitoring point's phase1/
    phase2 entries (steady_state_pipeline._point_phase_summary's shape) -
    shared by report.py/app.py so both display the same numbers computed
    the same way.

    Prefers each phase's T_inf_extrapolated (fit_asymptotic_value) over
    the plain windowed T_ss, mirroring exactly how the room-average
    reduction_pct already does this (see steady_state_pipeline.
    run_steady_state_scenario's own "using_extrapolated" logic) - a
    windowed average is a biased estimate of the true steady state
    whenever a curve hasn't fully flattened within the run's budget, and a
    monitoring point (a small, specific zone, often away from the main
    flow) is if anything MORE exposed to that than the room average is,
    not less. Only used when BOTH phases extrapolated successfully for
    THIS point - falls back to the windowed T_ss (both phases) otherwise,
    the same all-or-nothing rule the room level uses (mixing an
    extrapolated T1 with a windowed T2, or vice versa, would compare two
    different bases against each other).

    basis is "extrapolated_T_infinity" or "windowed_average" (matching the
    room level's own "ach_source" field's wording). Returns (None, None,
    None, basis) if either phase has no usable T_ss at all.
    """
    def _windowed_or_last_sample(p):
        if p.get("T_ss") is not None:
            return p["T_ss"]
        series = p.get("volAverage_T") or []
        return series[-1] if series else None

    Tinf1, Tinf2 = p1.get("T_inf_extrapolated"), p2.get("T_inf_extrapolated")
    using_extrapolated = Tinf1 is not None and Tinf2 is not None
    T1 = Tinf1 if using_extrapolated else _windowed_or_last_sample(p1)
    T2 = Tinf2 if using_extrapolated else _windowed_or_last_sample(p2)
    basis = "extrapolated_T_infinity" if using_extrapolated else "windowed_average"
    if T1 is None or T2 is None:
        return None, None, None, basis
    reduction_pct = (1 - T2 / T1) * 100 if T1 else None
    return T1, T2, reduction_pct, basis


def mixing_uniformity_note(result):
    """Compare each monitoring point's final T against the room-average
    final T for the same phase (steady-state) or the same end-of-curve
    point (decay). Returns a warning string if any point deviates by more
    than _MIXING_UNIFORMITY_THRESHOLD, else None (including when there are
    no monitoring points to compare against).

    Shared between app.py's Analysis tab and report.py's .docx export -
    both display the same results.json, so the same check applies to both.
    """
    monitoring = result.get("monitoring")
    if not monitoring:
        return None

    is_decay = "phase1" not in next(iter(monitoring.values()))
    deviations = []
    if not is_decay:
        for phase_key, phase_label, room_val in (
            ("phase1", "Phase 1", (result.get("phase1") or {}).get("T_ss")),
            ("phase2", "Phase 2", (result.get("phase2") or {}).get("T_ss")),
        ):
            if not room_val:
                continue
            for name, data in monitoring.items():
                # The trailing-window mean (same statistic room_val itself
                # is - see steady_state_pipeline._point_phase_summary), not
                # the single last raw iteration - a steady-state point's
                # curve[-1] can swing well off its own true plateau on a
                # turbulent run (see the live-volAverage validation), which
                # would make this check's verdict itself mostly noise.
                point_val = data[phase_key].get("T_ss")
                if point_val is None:
                    continue
                deviations.append((name, phase_label, point_val, (point_val - room_val) / room_val))
    else:
        decay_curve = result.get("decay_curve") or {}
        t_room, T_room = decay_curve.get("t_seconds"), decay_curve.get("volAverage_T")
        # Trailing-window mean (same statistic used everywhere else in this
        # codebase - see decay_analysis.windowed_stats), not the single raw
        # last sample - a decay curve run well past its target reduction
        # (e.g. for diagnostic purposes) can have both room-average and
        # point values sitting deep in solver/floating-point noise-floor
        # territory by the final sample, making a last-sample comparison
        # mostly noise rather than a real spatial-uniformity signal.
        room_val = windowed_stats(t_room, T_room)[0] if t_room and T_room else None
        if room_val:
            for name, data in monitoring.items():
                t_point, T_point = data.get("t_seconds"), data.get("volAverage_T")
                if not t_point or not T_point:
                    continue
                point_val = windowed_stats(t_point, T_point)[0]
                deviations.append((name, "final", point_val, (point_val - room_val) / room_val))

    flagged = [d for d in deviations if abs(d[3]) >= _MIXING_UNIFORMITY_THRESHOLD]
    if not flagged:
        return None

    # Decay mode has a single continuous curve (phase_label is always the
    # placeholder "final", not a real distinction worth reporting) - drop
    # the phase suffix entirely there; steady-state genuinely has two
    # distinct phases, so keeps it.
    if is_decay:
        parts = [f"'{name}' is {abs(pct * 100):.0f}% {'below' if pct < 0 else 'above'} the room average"
                  for name, phase_label, point_val, pct in flagged]
        return ("Note: the room is NOT well mixed - " + "; ".join(parts) + ". These values should "
                "be taken into account when doing localized risk considerations.")

    parts = [f"'{name}' is {abs(pct * 100):.0f}% {'below' if pct < 0 else 'above'} the room "
             f"average ({phase_label})" for name, phase_label, point_val, pct in flagged]
    return ("Note: the room is NOT well mixed - " + "; ".join(parts) + ". Room-average "
            "volAverage(T) should not be read as representative of concentration at any "
            "specific location; use the monitoring-location values for occupant-specific "
            "exposure estimates instead.")
