"""Optional mixing fan: a meanVelocityForce fvOption applying a body force
within a small cylindrical cellZone to maintain a target mean velocity
there, e.g. a ceiling fan aimed downward to improve room mixing.

Deliberately not an actuator-disk thrust model (Cp/Ct coefficients) - a
real fan in an otherwise-still room has no meaningful upstream flow for
that model to act on (it assumes air already moving *through* the disk,
like wind through a turbine, not a fan generating flow from stillness).
meanVelocityForce sidesteps this: it directly forces the zone's average
velocity to a target, whatever body force that takes.

Unlike the UV/contaminant-source scalarSemiImplicitSource entries, this
acts on U within the main solve itself (not through the scalarTransport
function object), so it's relevant during flow convergence too - a real
fan affects the converged flow field, not just the later scalar-transport
phases. constant/fvOptions is auto-loaded by any solver directly (see
run_pipeline.converge_flow_field's docstring), so no controlDict splicing
needed here either.

Verified empirically against a real solve (see commit history): the
nested <type>Coeffs{} wrapping style (matching this module's own
scalarSemiImplicitSource entries elsewhere) works, confirmed by the log's
"Pressure gradient source: uncorrected Ubar = ..." line showing the body
force genuinely driving the zone's velocity, not silently ignored.
"""

SPEED_RANGE = (0.05, 0.5)  # m/s, typical ceiling fan induced-velocity range


def fan_topo_set_dict(p1, p2, radius, zone_name="fanZone", cellset_name="fanZoneCells"):
    """topoSetDict actions carving a cylindrical cellZone (fan blade sweep
    disk) via cylinderToCell -> cellZoneSet, same two-step pattern as
    contaminant_source.py's box source.

    p1, p2: cylinder base/top center points, e.g. p1=(cx,cy,z0),
    p2=(cx,cy,z0+0.2) for a thin disk near the ceiling.
    radius: fan blade sweep radius [m].
    """
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      topoSetDict;", "}", "",
        "actions", "(",
        "    {", f"        name    {cellset_name};", "        type    cellSet;",
        "        action  new;", "        source  cylinderToCell;",
        f"        p1      ({p1[0]:.6g} {p1[1]:.6g} {p1[2]:.6g});",
        f"        p2      ({p2[0]:.6g} {p2[1]:.6g} {p2[2]:.6g});",
        f"        radius  {radius:.6g};",
        "    }",
        "    {", f"        name    {zone_name};", "        type    cellZoneSet;",
        "        action  new;", "        source  setToCellZone;",
        f"        set     {cellset_name};", "    }",
        ");", "",
    ]
    return "\n".join(lines)


def write_fan_topo_set_dict(case_dir, p1, p2, radius, zone_name="fanZone",
                             cellset_name="fanZoneCells", filename="fanTopoSetDict"):
    path = f"{case_dir}/system/{filename}"
    with open(path, "w") as f:
        f.write(fan_topo_set_dict(p1, p2, radius, zone_name, cellset_name))
    return path


def clamp_fan_speed(speed):
    lo, hi = SPEED_RANGE
    return max(lo, min(hi, speed))


def fan_fvoptions_entry(speed, direction=(0, 0, -1), zone_name="fanZone",
                         field_name="U", entry_name="fanSource"):
    """fvOptions entry text for the fan's meanVelocityForce. speed [m/s]
    should be within SPEED_RANGE (typical ceiling fan induced velocity);
    silently clamped if outside that range rather than erroring, since the
    GUI's own fan-speed slider (app.py) is already range-limited too.
    """
    speed = clamp_fan_speed(speed)
    dx, dy, dz = direction
    mag = (dx ** 2 + dy ** 2 + dz ** 2) ** 0.5
    dx, dy, dz = dx / mag, dy / mag, dz / mag
    ux, uy, uz = speed * dx, speed * dy, speed * dz

    lines = [
        f"{entry_name}",
        "{",
        "    type            meanVelocityForce;",
        "    active          true;",
        "",
        "    meanVelocityForceCoeffs",
        "    {",
        "        selectionMode   cellZone;",
        f"        cellZone        {zone_name};",
        f"        fields          ({field_name});",
        f"        Ubar            ({ux:.6g} {uy:.6g} {uz:.6g});",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)
