"""Generate system/volAverageDict: function objects tracking a field's
volume average (whole room) and patch average (e.g. outlet - the actual
exhaust concentration leaving the room, which can differ meaningfully from
the room average in an imperfectly mixed space). Not wired into
controlDict's functions{} (same as before) - run via
`postProcess -dict system/volAverageDict` after a solve completes, so the
resulting series is only as dense as controlDict's writeInterval.

live_vol_average_functions() below is a separate, additive path: the same
kind of function objects, but meant to be spliced into controlDict's live
functions{} block (see splice.splice_into_functions_block()) so they run
every solver iteration, independent of writeInterval - see the
live-volAverage validation experiment.
"""
from .splice import splice_into_functions_block as _splice_into_functions_block
from .wsl_utils import (
    write_case_file as _write_case_file,
    read_case_file as _read_case_file,
)


def vol_average_dict(field="T", patches=("outlet",)):
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      volAverageDict;", "}", "",
        "functions", "{",
        f"    read{field}", "    {",
        "        type            readFields;",
        '        libs            ("libfieldFunctionObjects.so");',
        f"        fields          ({field});",
        "        executeControl  timeStep;",
        "        executeInterval 1;",
        "    }", "",
        "    volAverage1", "    {",
        "        type            volFieldValue;",
        '        libs            ("libfieldFunctionObjects.so");',
        f"        fields          ({field});",
        "        operation       volAverage;",
        "        regionType      all;",
        "        executeControl  timeStep;",
        "        executeInterval 1;",
        "        writeControl    timeStep;",
        "        writeInterval   1;",
        "        writeFields     false;",
        "    }",
    ]
    for patch in patches:
        lines += [
            "", f"    {patch}Average", "    {",
            "        type            surfaceFieldValue;",
            '        libs            ("libfieldFunctionObjects.so");',
            f"        fields          ({field});",
            "        operation       areaAverage;",
            "        regionType      patch;",
            f"        name            {patch};",
            "        executeControl  timeStep;",
            "        executeInterval 1;",
            "        writeControl    timeStep;",
            "        writeInterval   1;",
            "        writeFields     false;",
            "    }",
        ]
    lines += ["}", ""]
    return "\n".join(lines)


def write_vol_average_dict(case_dir, field="T", patches=("outlet",)):
    path = f"{case_dir}/system/volAverageDict"
    _write_case_file(case_dir, "system/volAverageDict", vol_average_dict(field, patches))
    return path


def live_vol_average_functions(field="T", patches=(), monitoring_zones=(), indent="    "):
    """Splice-ready controlDict `functions{}` entries (no FoamFile/functions
    wrapper - unlike vol_average_dict()/monitoring_average_dict()) tracking
    `field` live, every solver iteration, instead of via a separate
    `postProcess` pass after the solve - see splice.splice_into_functions_block().
    Named with a "Live" suffix so these never collide with the existing
    postProcess-based volAverage1/<patch>Average/monitor_<zone> objects.

    No readFields object: unlike a standalone `postProcess` invocation
    (which runs in a fresh process with no field loaded), a live function
    object already has `field` resident in the running solver.

    monitoring_zones: names of cellZones already carved (e.g. via
    monitoring_points.write_monitoring_topo_set_dict) - must exist before
    the solver starts for a live regionType=cellZone object to find them.
    """
    lines = [
        "volAverageLive1", "{",
        "    type            volFieldValue;",
        '    libs            ("libfieldFunctionObjects.so");',
        f"    fields          ({field});",
        "    operation       volAverage;",
        "    regionType      all;",
        "    executeControl  timeStep;",
        "    executeInterval 1;",
        "    writeControl    timeStep;",
        "    writeInterval   1;",
        "    writeFields     false;",
        "}",
    ]
    for patch in patches:
        lines += [
            "", f"{patch}AverageLive", "{",
            "    type            surfaceFieldValue;",
            '    libs            ("libfieldFunctionObjects.so");',
            f"    fields          ({field});",
            "    operation       areaAverage;",
            "    regionType      patch;",
            f"    name            {patch};",
            "    executeControl  timeStep;",
            "    executeInterval 1;",
            "    writeControl    timeStep;",
            "    writeInterval   1;",
            "    writeFields     false;",
            "}",
        ]
    for zone in monitoring_zones:
        lines += [
            "", f"monitor_{zone}Live", "{",
            "    type            volFieldValue;",
            '    libs            ("libfieldFunctionObjects.so");',
            f"    fields          ({field});",
            "    operation       volAverage;",
            "    regionType      cellZone;",
            f"    name            {zone};",
            "    executeControl  timeStep;",
            "    executeInterval 1;",
            "    writeControl    timeStep;",
            "    writeInterval   1;",
            "    writeFields     false;",
            "}",
        ]
    return "\n".join(indent + line if line else "" for line in lines)


def splice_live_vol_average_if_needed(case_dir, field="T", patches=(), monitoring_zones=()):
    """Idempotently splice live_vol_average_functions() into case_dir's
    controlDict - decay mode's equivalent of steady_state_pipeline._run_phase's
    own inline idempotency check, pulled out into one shared helper so
    every decay-mode caller (the main UV-on run, and the UV-off control run
    - see ventilation_control.prepare_ventilation_only_control) gets the
    same "every timestep, not just full-field writes" tracking steady-state
    already had, without duplicating the check 5 times over.

    Safe to call even when controlDict already has the live block - e.g. a
    UV-off control run clones its case_dir from a case that may already
    have spliced this in, and splicing a second, identically-named copy
    would produce a brace-breaking duplicate rather than a no-op. Skips
    instead of raising in that case.
    """
    content = _read_case_file(case_dir, "system/controlDict")
    if "volAverageLive1" in content:
        return
    block = live_vol_average_functions(field=field, patches=patches, monitoring_zones=monitoring_zones)
    _, n_open, n_close = _splice_into_functions_block(case_dir, block)
    if n_open != n_close:
        raise RuntimeError(f"Brace mismatch after live-volAverage splice: open={n_open} close={n_close}")
