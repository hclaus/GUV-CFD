"""Generate 0/ initial condition field files for the topoSet-carved 8-patch
mesh (inlet, outlet, xMinWall, xMaxWall, floor, ceiling, frontWall, backWall).

Boundary condition types/values are ported from the original working case
(roomVent_scalar_uv): same physics setup (inlet velocity/turbulence, wall
functions, T=1 initial contamination decaying via a clean-air inlet), just
with leftWall/rightWall renamed to xMinWall/xMaxWall, and internalField
reset to uniform values since the original's internalField was solved data
copied from a later timestep on a *different* mesh (different cell count/
topology) - not valid to reuse directly.
"""

_WALL_PATCHES = ("xMinWall", "xMaxWall", "floor", "ceiling", "frontWall", "backWall")


def compute_inlet_velocity(ach, room_volume, inlet_area):
    """Inlet velocity magnitude [m/s] to achieve a target air-change rate.

    ach: air changes per hour [1/hr] (e.g. 3.0)
    room_volume: room volume [m^3]
    inlet_area: inlet opening area [m^2]

    Flow rate doesn't scale with room size for free - a fixed inlet velocity
    gives a fixed volumetric flow rate regardless of room volume, so ACH
    silently drifts as the room changes. This ties inlet velocity to ACH
    directly instead. (Sanity check: the original hand-tuned case used a
    fixed 0.278 m/s inlet on a 30 m^3 room with a 0.09 m^2 opening, which
    this formula reproduces almost exactly - implied ACH = 3.0024.)
    """
    flow_rate = ach * room_volume / 3600.0  # m^3/s
    return flow_rate / inlet_area


_FIELD_SPECS = {
    "U": {
        "foam_class": "volVectorField",
        "dimensions": "[0 1 -1 0 0 0 0]",
        "internal": "uniform (0 0 0)",
        "inlet": ("fixedValue", "uniform (0.278 0 0)"),
        "outlet": ("inletOutlet", None, "inletValue uniform (0 0 0);\n        value           uniform (0 0 0);"),
        "wall": ("noSlip", None),
    },
    "p": {
        "foam_class": "volScalarField",
        "dimensions": "[0 2 -2 0 0 0 0]",
        "internal": "uniform 0",
        "inlet": ("zeroGradient", None),
        "outlet": ("fixedValue", "uniform 0"),
        "wall": ("zeroGradient", None),
    },
    "k": {
        "foam_class": "volScalarField",
        "dimensions": "[0 2 -2 0 0 0 0]",
        "internal": "uniform 0.0039",
        "inlet": ("fixedValue", "uniform 0.0039"),
        "outlet": ("inletOutlet", None, "inletValue uniform 0.001;\n        value           uniform 0.001;"),
        "wall": ("kqRWallFunction", "uniform 1e-5"),
    },
    "omega": {
        "foam_class": "volScalarField",
        "dimensions": "[0 0 -1 0 0 0 0]",
        "internal": "uniform 5.43",
        "inlet": ("fixedValue", "uniform 5.43"),
        "outlet": ("inletOutlet", None, "inletValue uniform 5.43;\n        value           uniform 5.43;"),
        "wall": ("omegaWallFunction", "uniform 5.43"),
    },
    "nut": {
        "foam_class": "volScalarField",
        "dimensions": "[0 2 -1 0 0 0 0]",
        "internal": "uniform 0",
        "inlet": ("calculated", "uniform 0"),
        "outlet": ("calculated", "uniform 0"),
        "wall": ("nutkWallFunction", "uniform 0"),
    },
    "T": {
        "foam_class": "volScalarField",
        "dimensions": "[0 0 0 0 0 0 0]",
        "internal": "uniform 1",
        "inlet": ("fixedValue", "uniform 0"),
        "outlet": ("zeroGradient", None),
        "wall": ("zeroGradient", None),
    },
}


def _patch_block(spec_entry):
    if len(spec_entry) == 3:
        bc_type, _, extra = spec_entry
        return [f"        type            {bc_type};", f"        {extra}"]
    bc_type, value = spec_entry
    lines = [f"        type            {bc_type};"]
    if value is not None:
        lines.append(f"        value           {value};")
    return lines


def _field_spec(field_name, inlet_velocity, T_initial=1):
    spec = _FIELD_SPECS[field_name]
    if field_name == "U":
        vx, vy, vz = inlet_velocity
        spec = {**spec, "inlet": ("fixedValue", f"uniform ({vx:.6g} {vy:.6g} {vz:.6g})")}
    elif field_name == "T":
        spec = {**spec, "internal": f"uniform {T_initial:.6g}"}
    return spec


def boundary_field_block(field_name, inlet_velocity=(0.278, 0, 0), T_initial=1):
    """Return just the 'boundaryField { ... }' lines for a field."""
    spec = _field_spec(field_name, inlet_velocity, T_initial)
    lines = ["boundaryField", "{", "    inlet", "    {"]
    lines += ["    " + l for l in _patch_block(spec["inlet"])]
    lines += ["    }", "    outlet", "    {"]
    lines += ["    " + l for l in _patch_block(spec["outlet"])]
    lines += ["    }"]
    for patch in _WALL_PATCHES:
        lines += [f"    {patch}", "    {"]
        lines += ["    " + l for l in _patch_block(spec["wall"])]
        lines += ["    }"]
    lines += ["}", ""]
    return "\n".join(lines)


def field_file_content(field_name, time_dir="0", inlet_velocity=(0.278, 0, 0), T_initial=1):
    spec = _field_spec(field_name, inlet_velocity, T_initial)
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        f"    class       {spec['foam_class']};", f'    location    "{time_dir}";',
        f"    object      {field_name};", "}", "",
        f"dimensions      {spec['dimensions']};", "",
        f"internalField   {spec['internal']};", "",
    ]
    return "\n".join(lines) + "\n" + boundary_field_block(field_name, inlet_velocity, T_initial)


def write_initial_fields(case_dir, time_dir="0", inlet_velocity=(0.278, 0, 0), T_initial=1):
    """Write U, p, k, omega, nut, T into <case_dir>/<time_dir>/. Returns written paths.

    inlet_velocity: (vx, vy, vz) in m/s - see compute_inlet_velocity() to
    derive this from a target ACH and room volume.
    T_initial: T's starting internalField value - 1 for a one-time decay
    scenario (room starts fully contaminated), 0 for a steady-state
    build-up scenario (room starts clean, a continuous source fills it).
    """
    paths = {}
    for field_name in _FIELD_SPECS:
        path = f"{case_dir}/{time_dir}/{field_name}"
        with open(path, "w") as f:
            f.write(field_file_content(field_name, time_dir, inlet_velocity=inlet_velocity, T_initial=T_initial))
        paths[field_name] = path
    return paths


_FULL_RESET_FIELDS = ("T",)  # scalars representing a scenario's *starting*
# state (e.g. "room fully contaminated") rather than a flow-development
# quantity - mapFields' internal-field value for these means nothing (it's
# whatever the source case happened to have, not a "converged" state to
# reuse), so these get their internalField reset too, not just boundaryField.


def restore_boundary_conditions(case_dir, time_dir="0", inlet_velocity=(0.278, 0, 0), T_initial=1):
    """Reset the boundaryField{} section of each already-written field file
    back to our own BCs, leaving internalField untouched for flow fields
    (U/p/k/omega/nut) - but fully resetting fields in _FULL_RESET_FIELDS
    (T), internalField included, since mapping those from another case's
    state doesn't make physical sense.

    mapFields overwrites boundary patch values too for any patch it treats
    as a "cutting patch" (interpolating from the source's internal field
    rather than a same-named source patch) - which corrupts a fixedValue
    inlet like ours (spatially-varying garbage instead of the ACH-derived
    velocity). Call this right after mapFields to undo that damage while
    keeping the internal-field mapping it was actually meant to provide
    (for the fields where that mapping is meaningful).
    """
    paths = {}
    for field_name in _FIELD_SPECS:
        path = f"{case_dir}/{time_dir}/{field_name}"
        if field_name in _FULL_RESET_FIELDS:
            with open(path, "w") as f:
                f.write(field_file_content(field_name, time_dir, inlet_velocity=inlet_velocity, T_initial=T_initial))
            paths[field_name] = path
            continue
        with open(path) as f:
            content = f.read()
        idx = content.index("boundaryField")
        new_content = content[:idx] + boundary_field_block(field_name, inlet_velocity)
        with open(path, "w") as f:
            f.write(new_content)
        paths[field_name] = path
    return paths
