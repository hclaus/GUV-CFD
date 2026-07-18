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
import math

import numpy as np

from .case_io import read_patch_face_centers

_WALL_PATCHES = ("xMinWall", "xMaxWall", "floor", "ceiling", "frontWall", "backWall")

# Inward-facing unit normal for each wall an opening can be placed on (see
# mesh_gen._WALL_SPECS for the matching opening-geometry table) - the
# single shared source of truth for "which way does air entering through
# this wall point", previously duplicated independently in run_pipeline.py
# and ventilation_control.py (a real drift risk even before this dict grew
# from 2 to 6 walls).
WALL_INFLOW_DIRECTION = {
    "xMin": (1, 0, 0), "xMax": (-1, 0, 0),
    "frontWall": (0, 1, 0), "backWall": (0, -1, 0),
    "floor": (0, 0, 1), "ceiling": (0, 0, -1),
}


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


def compute_inlet_velocities(ach, room_volume, openings):
    """Inlet velocity vector [m/s] for each of N active inlets.

    openings: list of (wall, area) for every active inlet. All inlets
    share the same velocity *magnitude* - the total ACH-derived flow rate
    divided by the combined inlet area - so flow splits naturally in
    proportion to each inlet's own opening area, each pointed into the
    room along its own wall's inward normal (WALL_INFLOW_DIRECTION).

    Returns a list of (vx,vy,vz), same order as `openings`.
    """
    total_area = sum(area for _, area in openings)
    v_mag = compute_inlet_velocity(ach, room_volume, total_area)
    return [tuple(v_mag * d for d in WALL_INFLOW_DIRECTION[wall]) for wall, _ in openings]


# Which two room dimensions are tangent to (in the plane of) each wall, in
# (x,y,z)-index form - mirrors mesh_gen._WALL_SPECS's own in-plane-axis
# model (same 6 walls, same axis pairing), needed here to project a
# radial-diffuser direction onto the wall's own plane rather than mesh_gen's
# private constant.
_WALL_IN_PLANE_AXES = {
    "xMin": (1, 2), "xMax": (1, 2),
    "frontWall": (0, 2), "backWall": (0, 2),
    "floor": (0, 1), "ceiling": (0, 1),
}


def _in_plane_basis(wall):
    """(ref1, ref2): world-axis-aligned unit vectors spanning the wall's
    tangent plane, in the SAME (a1, a2) order as mesh_gen._WALL_SPECS/
    opening_half_extents - e.g. ref1 along a1, ref2 along a2. Deriving
    this directly from the shared (a1, a2) convention (rather than
    independently, e.g. via an arbitrary seed vector + cross product)
    matters: an independently-derived basis can end up axis-*swapped*
    relative to mesh_gen's for some walls (confirmed for xMax/xMin) even
    though both are individually valid orthonormal bases - silently
    applying opening_half_extents' (half_w, half_h) to the wrong axis."""
    a1, a2 = _WALL_IN_PLANE_AXES[wall]
    ref1, ref2 = np.zeros(3), np.zeros(3)
    ref1[a1] = 1.0
    ref2[a2] = 1.0
    return ref1, ref2


def compute_radial_inlet_velocities(face_centers, opening_center, wall, v_mag, half_extents,
                                     surface_angle_deg=15, center_angle_deg=90):
    """Per-face velocity vectors [m/s] for a surface-attached (ceiling/wall)
    diffuser: air spreads radially outward from the opening's center,
    along the plane of the mounting wall (Coanda-effect ceiling
    attachment), tilted toward `surface_angle_deg` down into the room near
    the opening's edge - instead of every face sharing one "direct jet"
    vector straight into the room.

    Validated in practice (Srebric & Chen 2002, HVAC&R Research) for
    round/square ceiling, vortex, and grille diffusers - not for
    multi-jet diffusers (slot/nozzle/valve), which need real measured jet
    profiles instead of a fixed formula.

    face_centers: (N,3) array/list, e.g. from case_io.read_patch_face_centers.
    opening_center: (x,y,z) of the opening's own center (not the room's).
    half_extents: (half_width, half_height) of the opening's TRUE physical
    size (see mesh_gen.opening_half_extents) in the wall's (a1, a2)
    in-plane axis order - not derived from face_centers' own sampled
    extent, which under-reaches the true edge by half a cell (mesh faces
    are inset from the physical boundary).
    v_mag: same magnitude on every face (total flow rate conserved,
    uniform-mesh faces are similar size).

    Directions are NOT simply each face's own literal (face_center -
    opening_center) offset, normalized - that seemingly obvious approach
    has real problems, confirmed against a real failing case:

    (1) It's singular at the opening's exact center (direction undefined
    at r=0, rotating through the full circle in an arbitrarily small
    neighborhood around it), so two faces immediately straddling the
    center - completely ordinary on a mesh whose face count happens to be
    even along an axis - get assigned near-opposite directions despite
    being physically adjacent: a velocity discontinuity sharp enough to
    destabilize the downstream scalar transport solve. This is genuine
    physics near a real diffuser's exact center too (radial spread really
    does reverse crossing straight through it) - but a real diffuser also
    has a solid physical hub/vane structure right at that center, not an
    open-air discontinuity, so nothing pushes hard in either direction
    right there. Modeled here by blending each face's tilt from
    `center_angle_deg` (90 degrees - straight into the room, no radial
    component at all) at r=0 up to `surface_angle_deg` (mostly radial
    spread) at the opening's outer edge, rather than a single fixed angle
    everywhere - faces near the center get a small, gentle nudge in
    whichever direction rather than a full-strength push, so neighbors
    straddling the center no longer differ by anywhere near full
    magnitude, while faces near the edge keep the originally-validated
    momentum-method radial spread. r=0..1 is measured in the opening's own
    *shape-normalized* coordinates (each in-plane axis divided by its own
    half-extent) rather than raw distance, so an elongated rectangular
    opening still reaches full spread at its (nearer) short edge, not only
    at its long edge.

    (2) It makes covered angles entirely dependent on the mesh's discrete
    face layout - an even-width grid, for instance, never puts a face
    exactly on a cardinal axis, so no face ever points straight out, which
    looks (and is) wrong for a diffuser meant to push air uniformly in
    *every* direction, not just wherever the mesh happened to sample.
    Fixed by computing each face's angle from the SAME shape-normalized
    coordinates used for the radial taper above (stretching a rectangular
    opening into a unit circle before taking its angle) instead of the
    raw, mesh-dependent offset - this is a purely local, continuous
    per-face formula (no need to know about any other face), and any face
    sitting exactly on the opening's actual midline (not just one
    arbitrarily-chosen mesh face) comes out exactly cardinal.

    Returns a list of (vx,vy,vz), same order as `face_centers`.
    """
    normal = np.array(WALL_INFLOW_DIRECTION[wall], dtype=float)
    center = np.array(opening_center, dtype=float)
    ref1, ref2 = _in_plane_basis(wall)
    hw, hh = half_extents

    face_centers = np.asarray(face_centers, dtype=float)
    n = len(face_centers)
    deltas = face_centers - center
    in_plane = deltas - np.outer(deltas @ normal, normal)
    # Shape-normalized coordinates: stretch the opening's actual rectangle
    # into a unit circle (u,v in [-1,1]) before measuring angle/radius, so
    # both the angle and the center->edge taper respect the opening's own
    # aspect ratio instead of raw (mesh- and shape-dependent) distance.
    u = (in_plane @ ref1) / hw if hw > 0 else in_plane @ ref1 * 0
    v = (in_plane @ ref2) / hh if hh > 0 else in_plane @ ref2 * 0
    r = np.sqrt(u ** 2 + v ** 2)
    at_center = r < 1e-9

    velocities = []
    for i in range(n):
        if at_center[i]:
            # face center coincides with the opening center (e.g. a
            # degenerate/point-like opening) - no radial direction to
            # take, fall back to straight into the room.
            direction = normal
        else:
            r_norm = min(r[i], 1.0)  # 0 at center, 1 at (or beyond) the true edge
            tilt = math.radians(center_angle_deg - (center_angle_deg - surface_angle_deg) * r_norm)
            radial = (u[i] * ref1 + v[i] * ref2) / r[i]
            direction = radial * math.cos(tilt) + normal * math.sin(tilt)
        # float(...) - not np.float64 - so this list is directly
        # JSON-serializable once it lands in a results.json/summary dict,
        # same as every other plain-tuple velocity in this codebase.
        velocities.append(tuple(float(v) for v in v_mag * direction))
    return velocities


def resolve_inlet_velocity(case_dir, patch_name, wall, opening_center, v_mag, diffuser_type="direct",
                            half_extents=None):
    """The inlet velocity to use for `patch_name`'s U boundary condition -
    a plain (vx,vy,vz) tuple ("direct jet", today's only behavior) or a
    per-face list of tuples ("ceiling"/surface-attached diffuser - see
    compute_radial_inlet_velocities).

    half_extents: required for diffuser_type="ceiling" - see
    mesh_gen.opening_half_extents (the opening's true physical
    half-width/half-height, in the wall's (a1, a2) axis order).

    Stateless and cheap (a local polyMesh file parse, no WSL round-trip) -
    a caller that needs this at multiple points in a run (the initial
    write, a post-mapFields restore, steady-state's phase-1 restore) just
    calls it again each time rather than threading a precomputed value
    through; the mesh/opening geometry doesn't change during a run.
    """
    if diffuser_type == "direct":
        return tuple(v_mag * d for d in WALL_INFLOW_DIRECTION[wall])
    if diffuser_type == "ceiling":
        face_centers = read_patch_face_centers(case_dir, patch_name)
        return compute_radial_inlet_velocities(face_centers, opening_center, wall, v_mag, half_extents)
    raise ValueError(f"Unknown diffuser_type: {diffuser_type!r} (expected 'direct' or 'ceiling')")


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


def _is_per_face_velocity(inlet_velocity):
    """True for a surface-attached diffuser's per-face list of (vx,vy,vz)
    tuples (see resolve_inlet_velocity), False for a plain single
    (vx,vy,vz) tuple ("direct jet", today's only shape before this)."""
    return len(inlet_velocity) > 0 and isinstance(inlet_velocity[0], (tuple, list, np.ndarray))


def _nonuniform_vector_block(values):
    """`nonuniform List<vector> N (\\n(x y z)\\n...\\n)` text for a
    boundaryField value - the per-face vector analog of case_io.
    write_scalar_field's `nonuniform List<scalar>` internalField pattern.
    """
    lines = [f"nonuniform List<vector> ", str(len(values)), "("]
    lines += [f"({vx:.6g} {vy:.6g} {vz:.6g})" for vx, vy, vz in values]
    lines.append(")")
    return "\n        ".join(lines)


def _inlet_velocity_value(inlet_velocity):
    if _is_per_face_velocity(inlet_velocity):
        return _nonuniform_vector_block(inlet_velocity)
    vx, vy, vz = inlet_velocity
    return f"uniform ({vx:.6g} {vy:.6g} {vz:.6g})"


def _field_spec(field_name, inlet_velocity, T_initial=1):
    spec = _FIELD_SPECS[field_name]
    if field_name == "U":
        spec = {**spec, "inlet": ("fixedValue", _inlet_velocity_value(inlet_velocity))}
    elif field_name == "T":
        spec = {**spec, "internal": f"uniform {T_initial:.6g}"}
    return spec


def boundary_field_block(field_name, inlet_velocity=(0.278, 0, 0), T_initial=1,
                          inlet2_velocity=None, has_outlet2=False):
    """Return just the 'boundaryField { ... }' lines for a field.

    inlet2_velocity: if given, emit a 2nd inlet patch block too - same BC
    template as the primary inlet, just its own velocity for U (every
    other field's inlet BC is a fixed constant regardless of which
    physical inlet it is, so its block is identical to the primary one's).
    has_outlet2: if True, emit a 2nd outlet patch block, identical to the
    primary outlet in every field (outlets are passive inletOutlet/
    zeroGradient - there's no per-instance value to vary).
    """
    spec = _field_spec(field_name, inlet_velocity, T_initial)
    lines = ["boundaryField", "{", "    inlet", "    {"]
    lines += ["    " + l for l in _patch_block(spec["inlet"])]
    lines += ["    }"]
    if inlet2_velocity is not None:
        spec2 = _field_spec(field_name, inlet2_velocity, T_initial)
        lines += ["    inlet2", "    {"]
        lines += ["    " + l for l in _patch_block(spec2["inlet"])]
        lines += ["    }"]
    lines += ["    outlet", "    {"]
    lines += ["    " + l for l in _patch_block(spec["outlet"])]
    lines += ["    }"]
    if has_outlet2:
        lines += ["    outlet2", "    {"]
        lines += ["    " + l for l in _patch_block(spec["outlet"])]
        lines += ["    }"]
    for patch in _WALL_PATCHES:
        lines += [f"    {patch}", "    {"]
        lines += ["    " + l for l in _patch_block(spec["wall"])]
        lines += ["    }"]
    lines += ["}", ""]
    return "\n".join(lines)


def field_file_content(field_name, time_dir="0", inlet_velocity=(0.278, 0, 0), T_initial=1,
                        inlet2_velocity=None, has_outlet2=False):
    spec = _field_spec(field_name, inlet_velocity, T_initial)
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        f"    class       {spec['foam_class']};", f'    location    "{time_dir}";',
        f"    object      {field_name};", "}", "",
        f"dimensions      {spec['dimensions']};", "",
        f"internalField   {spec['internal']};", "",
    ]
    return "\n".join(lines) + "\n" + boundary_field_block(
        field_name, inlet_velocity, T_initial, inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2)


def write_initial_fields(case_dir, time_dir="0", inlet_velocity=(0.278, 0, 0), T_initial=1,
                          inlet2_velocity=None, has_outlet2=False):
    """Write U, p, k, omega, nut, T into <case_dir>/<time_dir>/. Returns written paths.

    inlet_velocity: (vx, vy, vz) in m/s - see compute_inlet_velocity() to
    derive this from a target ACH and room volume.
    T_initial: T's starting internalField value - 1 for a one-time decay
    scenario (room starts fully contaminated), 0 for a steady-state
    build-up scenario (room starts clean, a continuous source fills it).
    inlet2_velocity/has_outlet2: an optional 2nd inlet/outlet - see
    boundary_field_block.
    """
    paths = {}
    for field_name in _FIELD_SPECS:
        path = f"{case_dir}/{time_dir}/{field_name}"
        with open(path, "w") as f:
            f.write(field_file_content(field_name, time_dir, inlet_velocity=inlet_velocity, T_initial=T_initial,
                                        inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2))
        paths[field_name] = path
    return paths


_FULL_RESET_FIELDS = ("T",)  # scalars representing a scenario's *starting*
# state (e.g. "room fully contaminated") rather than a flow-development
# quantity - mapFields' internal-field value for these means nothing (it's
# whatever the source case happened to have, not a "converged" state to
# reuse), so these get their internalField reset too, not just boundaryField.


def restore_boundary_conditions(case_dir, time_dir="0", inlet_velocity=(0.278, 0, 0), T_initial=1,
                                 inlet2_velocity=None, has_outlet2=False):
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

    inlet2_velocity/has_outlet2: an optional 2nd inlet/outlet - see
    boundary_field_block.
    """
    paths = {}
    for field_name in _FIELD_SPECS:
        path = f"{case_dir}/{time_dir}/{field_name}"
        if field_name in _FULL_RESET_FIELDS:
            with open(path, "w") as f:
                f.write(field_file_content(field_name, time_dir, inlet_velocity=inlet_velocity, T_initial=T_initial,
                                            inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2))
            paths[field_name] = path
            continue
        with open(path) as f:
            content = f.read()
        idx = content.index("boundaryField")
        new_content = content[:idx] + boundary_field_block(
            field_name, inlet_velocity, inlet2_velocity=inlet2_velocity, has_outlet2=has_outlet2)
        with open(path, "w") as f:
            f.write(new_content)
        paths[field_name] = path
    return paths
