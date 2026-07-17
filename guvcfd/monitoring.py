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
    with open(path, "w") as f:
        f.write(vol_average_dict(field, patches))
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
