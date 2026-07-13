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

from .decay_analysis import read_vol_average_dat, compute_effective_eACH
from .wsl_utils import wsl_path, run_wsl_or_raise

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
    """
    action_lines = []
    for p in points:
        zname = zone_name(p["name"])
        cellset_name = f"{zname}Cells"
        size = p["cells_per_side"] * cell_size
        cx, cy, cz = p["x"], p["y"], p["z"]
        lo = (cx - size / 2, cy - size / 2, cz - size / 2)
        hi = (cx + size / 2, cy + size / 2, cz + size / 2)
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
    with open(path, "w") as f:
        f.write(monitoring_topo_set_dict(points, cell_size))
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
    with open(path, "w") as f:
        f.write(monitoring_average_dict(points, field))
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
            eACH_eff, lambda_eff, intercept = compute_effective_eACH(t, T, ventilation_ach)
            entry["eACH_uv_effective"] = eACH_eff
        results[p["name"]] = entry
        suffix = f", eACH_uv={entry['eACH_uv_effective']:.4g}/hr" if "eACH_uv_effective" in entry else ""
        log_fn(f"  {p['name']}: {len(t)} points, final volAverage(T)={T[-1]:.4g}{suffix}")
    return results


# How far a monitoring point's final T can sit from the room-average final T
# before it's worth calling out - room-average volAverage(T) is a spatial
# average, and real rooms are rarely well mixed; a plain average number can
# badly misrepresent the concentration at any one occupied location.
_MIXING_UNIFORMITY_THRESHOLD = 0.15


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

    deviations = []
    if "phase1" in next(iter(monitoring.values())):
        for phase_key, phase_label, room_val in (
            ("phase1", "Phase 1", (result.get("phase1") or {}).get("T_ss")),
            ("phase2", "Phase 2", (result.get("phase2") or {}).get("T_ss")),
        ):
            if not room_val:
                continue
            for name, data in monitoring.items():
                curve = data[phase_key]["volAverage_T"]
                if not curve:
                    continue
                point_val = curve[-1]
                deviations.append((name, phase_label, point_val, (point_val - room_val) / room_val))
    else:
        curve = (result.get("decay_curve") or {}).get("volAverage_T")
        room_val = curve[-1] if curve else None
        if room_val:
            for name, data in monitoring.items():
                point_curve = data.get("volAverage_T")
                if not point_curve:
                    continue
                point_val = point_curve[-1]
                deviations.append((name, "final", point_val, (point_val - room_val) / room_val))

    flagged = [d for d in deviations if abs(d[3]) >= _MIXING_UNIFORMITY_THRESHOLD]
    if not flagged:
        return None

    parts = [f"'{name}' is {abs(pct * 100):.0f}% {'below' if pct < 0 else 'above'} the room "
             f"average ({phase_label})" for name, phase_label, point_val, pct in flagged]
    return ("Note: the room is NOT well mixed - " + "; ".join(parts) + ". Room-average "
            "volAverage(T) should not be read as representative of concentration at any "
            "specific location; use the monitoring-location values for occupant-specific "
            "exposure estimates instead.")
