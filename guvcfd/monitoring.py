"""Generate system/volAverageDict: function objects tracking a field's
volume average (whole room) and patch average (e.g. outlet - the actual
exhaust concentration leaving the room, which can differ meaningfully from
the room average in an imperfectly mixed space). Not wired into
controlDict's functions{} (same as before) - run via
`postProcess -dict system/volAverageDict` after a solve completes.
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
