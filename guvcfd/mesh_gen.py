"""Generate a simple single-block room mesh directly from an Illuminate room's
dimensions, with inlet/outlet openings carved out of two opposite walls via
topoSet + createPatch (rather than GenBlockmesh.py's hand-built multi-block
approach, which encodes the opening position in the block topology itself).

Sequence: blockMesh -> topoSet -> createPatch -overwrite -> checkMesh.
"""
import math

import numpy as np

# Face vertex winding matches GenBlockmesh.py's proven convention (validated
# against a real solve): v0..v3 at z=0 (floor layer), v4..v7 at z=Lz (ceiling
# layer), going around (x0,y0) (x1,y0) (x1,y1) (x0,y1) at each layer.
_HEX_VERTICES = [
    (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
    (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
]
_FACES = {
    "xMinWall": (0, 4, 7, 3),   # x = 0
    "xMaxWall": (1, 2, 6, 5),   # x = Lx
    "frontWall": (0, 1, 5, 4),  # y = 0
    "backWall": (3, 7, 6, 2),   # y = Ly
    "floor": (0, 3, 2, 1),      # z = 0
    "ceiling": (4, 5, 6, 7),    # z = Lz
}


def block_mesh_dict(Lx, Ly, Lz, cell_size=0.1):
    """Single-block box mesh covering the whole room, before opening carving."""
    nx = max(1, round(Lx / cell_size))
    ny = max(1, round(Ly / cell_size))
    nz = max(1, round(Lz / cell_size))

    vertices = [(vx * Lx, vy * Ly, vz * Lz) for vx, vy, vz in _HEX_VERTICES]

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      blockMeshDict;", "}", "",
        "scale 1;", "", "vertices", "(",
    ]
    for v in vertices:
        lines.append(f"    ({v[0]:.6g} {v[1]:.6g} {v[2]:.6g})")
    lines += [");", "", "blocks", "(",
              f"    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)",
              ");", "", "edges", "(", ");", "", "boundary", "("]
    for name, face in _FACES.items():
        lines += [
            f"    {name}", "    {", "        type wall;", "        faces", "        (",
            f"            ({face[0]} {face[1]} {face[2]} {face[3]})", "        );", "    }",
        ]
    lines += [");", "", "mergePatchPairs", "(", ");", ""]
    return "\n".join(lines)


# For each wall an opening can be placed on: (index of the axis normal to
# that wall in (x,y,z), that axis's position for this wall, indices of the
# two "in-plane" axes center_frac/size apply to, in that order). "xMin"/
# "xMax" keep their historical bare names (no "Wall" suffix) for backward
# compatibility with already-saved .guvcfd project files that store
# "inlet-wall": "xMin" etc; the other four match the wall-patch names
# _FACES/visualization._WALL_LABEL_POSITIONS already use.
_WALL_SPECS = {
    "xMin": (0, lambda Lx, Ly, Lz: 0.0, (1, 2)),
    "xMax": (0, lambda Lx, Ly, Lz: Lx, (1, 2)),
    "frontWall": (1, lambda Lx, Ly, Lz: 0.0, (0, 2)),
    "backWall": (1, lambda Lx, Ly, Lz: Ly, (0, 2)),
    "floor": (2, lambda Lx, Ly, Lz: 0.0, (0, 1)),
    "ceiling": (2, lambda Lx, Ly, Lz: Lz, (0, 1)),
}


def snap_outward(value, cell_size, direction, fp_tol=1e-9):
    """Snap `value` outward to the nearest cell_size grid line - floor for
    direction="lo", ceil for direction="hi" (see _opening_box's docstring
    for why outward, not to-nearest).

    fp_tol: nudges the value slightly toward its own nearest grid line
    before flooring/ceiling, so a value that's ALREADY grid-aligned but
    represented with tiny binary floating-point noise (e.g.
    12.999999999999998 instead of exactly 13.0) doesn't get spuriously
    pushed a full cell_size further out - only a value that's genuinely
    off-grid (by more than fp_tol) actually moves. 1e-9 is many orders of
    magnitude below any physically meaningful cell_size used here, so it
    can't mask a real (non-noise) misalignment.
    """
    n = value / cell_size
    if direction == "lo":
        return math.floor(n + fp_tol) * cell_size
    return math.ceil(n - fp_tol) * cell_size


def _opening_box(wall, Lx, Ly, Lz, center_frac, size, cell_size=None, eps=1e-4):
    """Return ((xmin,ymin,zmin),(xmax,ymax,zmax)) for a boxToFace opening on
    any of the 6 room walls (see _WALL_SPECS), centered at center_frac of
    the wall's two in-plane dimensions (fractions, in axis-index order -
    e.g. (y,z) for xMin/xMax, (x,y) for floor/ceiling), with the given
    (width, height) opening size.

    cell_size: if given, snap both in-plane edges OUTWARD to the mesh grid
    (floor the low edge, ceil the high edge) instead of using the raw
    center_frac/size arithmetic as-is. Without this, a nominal center/size
    combination that doesn't land on a whole number of cells (e.g. an odd
    cell count centered exactly on a mesh vertex, which can't be symmetric
    - 3 cells can't straddle a vertex evenly) produces box edges that
    coincide almost exactly with a face-center or vertex coordinate, which
    is a boxToFace floating-point boundary tie: some cells right at the
    edge get included, others excluded, essentially at random, producing a
    lopsided/irregular carved patch instead of a clean block.

    Snapping outward (rather than to-nearest) guarantees the carved patch
    always CONTAINS the requested opening, never shrinks it - confirmed
    this matters on a real case: a 0.3m opening on a 0.1m mesh is exactly
    3 cells (an odd count with no even split), so BOTH edges land exactly
    on a rounding tie, and round-to-nearest (Python's banker's rounding)
    can resolve both ties inward, silently carving a 0.2m opening (44% of
    the requested area) instead of 0.3m - with no error, no warning, and a
    downstream CFD delivery-rate check that looked like a real physical
    ventilation shortfall but was actually just this. Rounding to nearest
    also isn't even consistently wrong in the same direction: an identical
    tie elsewhere can round outward instead, growing the opening - the
    grow-or-shrink outcome depends on incidental integer parity, not
    anything physical. Snapping outward removes that ambiguity: the result
    is always >= the requested size (growing by up to one cell_size per
    edge in the worst case), which is a far safer failure mode than an
    invisible shortfall.
    """
    if wall not in _WALL_SPECS:
        raise ValueError(f"Unsupported wall {wall!r}, expected one of {sorted(_WALL_SPECS)}")
    normal_axis, normal_pos_fn, (a1, a2) = _WALL_SPECS[wall]
    dims = (Lx, Ly, Lz)
    c1, c2 = center_frac
    w, h = size
    lo, hi = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    lo[a1], hi[a1] = c1 * dims[a1] - w / 2, c1 * dims[a1] + w / 2
    lo[a2], hi[a2] = c2 * dims[a2] - h / 2, c2 * dims[a2] + h / 2
    if cell_size:
        lo[a1], hi[a1] = snap_outward(lo[a1], cell_size, "lo"), snap_outward(hi[a1], cell_size, "hi")
        lo[a2], hi[a2] = snap_outward(lo[a2], cell_size, "lo"), snap_outward(hi[a2], cell_size, "hi")
        if hi[a1] <= lo[a1]:
            hi[a1] = lo[a1] + cell_size
        if hi[a2] <= lo[a2]:
            hi[a2] = lo[a2] + cell_size
    pos = normal_pos_fn(Lx, Ly, Lz)
    lo[normal_axis], hi[normal_axis] = pos - eps, pos + eps
    return tuple(lo), tuple(hi)


def opening_center(wall, Lx, Ly, Lz, center_frac, size, cell_size=None):
    """The opening's own absolute (x,y,z) center - not the room's - for
    callers that need real-world coordinates (e.g. initial_fields.
    resolve_inlet_velocity's radial-diffuser direction computation)
    rather than just the boxToFace carving region _opening_box() builds.

    cell_size: must match what write_mesh_dicts() carved the opening with
    (see _opening_box's grid-snapping) - otherwise this returns the
    nominal, unsnapped center rather than the true center of the patch
    that actually got carved, throwing off the radial-diffuser direction
    math by up to half a cell.
    """
    lo, hi = _opening_box(wall, Lx, Ly, Lz, center_frac, size, cell_size=cell_size)
    return tuple((l + h) / 2 for l, h in zip(lo, hi))


def opening_half_extents(wall, Lx, Ly, Lz, center_frac, size, cell_size=None):
    """The opening's true (half_width, half_height) in its wall's two
    in-plane dimensions, in (a1, a2) axis order (see _WALL_SPECS) - from
    the same snapped box opening_center() derives its center from, so
    callers get the *actual* carved half-extents (which can differ
    slightly from the nominal `size` once grid-snapped) rather than
    reconstructing size/2 by hand and risking it drifting out of sync.

    Used by initial_fields.compute_radial_inlet_velocities to normalize
    each face's offset by the opening's real physical shape (stretching a
    rectangular opening into a unit circle before taking its angle) -
    using the true half-extents here, not just the mesh's own sampled
    face-position extremes, matters because mesh faces are inset by half
    a cell from the true physical edge, so inferring "how close to the
    edge is this face" from sampled data alone underestimates it.
    """
    lo, hi = _opening_box(wall, Lx, Ly, Lz, center_frac, size, cell_size=cell_size)
    _, _, (a1, a2) = _WALL_SPECS[wall]
    return (hi[a1] - lo[a1]) / 2, (hi[a2] - lo[a2]) / 2


def opening_grid_alignment(wall, Lx, Ly, Lz, center_frac, size, cell_size):
    """(nominal_size, actual_size) (width, height) tuples for an opening,
    comparing the raw requested size against what will actually be
    carved once its position/size snap to cell_size - lets a caller warn
    about size drift BEFORE running a simulation (see
    run_pipeline.check_settings_grid_alignment), rather than discovering
    it after the fact the way run_pipeline.check_ach_delivery does.
    """
    nominal = tuple(2 * v for v in opening_half_extents(wall, Lx, Ly, Lz, center_frac, size))
    actual = tuple(2 * v for v in opening_half_extents(wall, Lx, Ly, Lz, center_frac, size, cell_size=cell_size))
    return nominal, actual


def _face_set_action(name, box):
    (x0, y0, z0), (x1, y1, z1) = box
    box_str = f"({x0:.6g} {y0:.6g} {z0:.6g}) ({x1:.6g} {y1:.6g} {z1:.6g})"
    return [
        "    {", f"        name    {name}Faces;", "        type    faceSet;",
        "        action  new;", "        source  boxToFace;",
        f"        box     {box_str};", "    }",
    ]


def topo_set_dict(inlet_box, outlet_box, inlet2_box=None, outlet2_box=None):
    """inlet2_box/outlet2_box: an optional 2nd inlet/outlet opening (see
    mesh_gen._opening_box) - carved as additional uniquely-named faceSets
    (inlet2Faces/outlet2Faces) alongside the always-present inlet/outlet
    ones, mirroring monitoring_points.monitoring_topo_set_dict's "loop over
    N openings, emit N uniquely-named actions in one dict" pattern.
    """
    actions = _face_set_action("inlet", inlet_box) + _face_set_action("outlet", outlet_box)
    if inlet2_box is not None:
        actions += _face_set_action("inlet2", inlet2_box)
    if outlet2_box is not None:
        actions += _face_set_action("outlet2", outlet2_box)

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      topoSetDict;", "}", "",
        "actions", "(",
        *actions,
        ");", "",
    ]
    return "\n".join(lines)


def _patch_entry(name):
    return [
        "    {", f"        name        {name};", "        patchInfo", "        {",
        "            type patch;", "        }",
        "        constructFrom set;", f"        set         {name}Faces;", "    }",
    ]


def create_patch_dict(has_inlet2=False, has_outlet2=False):
    patches = _patch_entry("inlet") + _patch_entry("outlet")
    if has_inlet2:
        patches += _patch_entry("inlet2")
    if has_outlet2:
        patches += _patch_entry("outlet2")

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      createPatchDict;", "}", "",
        "pointSync false;", "", "patches", "(",
        *patches,
        ");", "",
    ]
    return "\n".join(lines)


def map_fields_dict(patch_names):
    """mapFieldsDict declaring every target patch a "cutting patch" (general
    internal-field-based interpolation, no source-patch name correspondence
    required). Needed because -consistent mode requires identical patch
    name/order between source and target, which a topoSet-carved mesh won't
    have against a differently-built source mesh - but since our own
    boundary conditions are already fully specified (fixedValue/noSlip/wall
    functions), we don't need patch-to-patch value transfer anyway, only the
    interior field.
    """
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      mapFieldsDict;", "}", "",
        "patchMap", "(", ");", "", "cuttingPatches", "(",
    ]
    lines += [f"    {p}" for p in patch_names]
    lines += [");", ""]
    return "\n".join(lines)


def write_map_fields_dict(case_dir, patch_names):
    path = f"{case_dir}/system/mapFieldsDict"
    with open(path, "w") as f:
        f.write(map_fields_dict(patch_names))
    return path


def write_mesh_dicts(case_dir, Lx, Ly, Lz, cell_size=0.1,
                      inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
                      outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
                      inlet2_wall=None, inlet2_center=None, inlet2_size=None,
                      outlet2_wall=None, outlet2_center=None, outlet2_size=None):
    """Write blockMeshDict, topoSetDict, createPatchDict into case_dir/system/.

    inlet/outlet center/size are fractions of the wall's two in-plane
    dimensions for center, and absolute meters for size (see
    _opening_box). inlet2_*/outlet2_*: an optional 2nd inlet/outlet, on any
    of the 6 walls independently of the primary one's wall - None (the
    default) means "no 2nd opening", carving the same 2-patch mesh as
    before this parameter existed.
    """
    inlet_box = _opening_box(inlet_wall, Lx, Ly, Lz, inlet_center, inlet_size, cell_size=cell_size)
    outlet_box = _opening_box(outlet_wall, Lx, Ly, Lz, outlet_center, outlet_size, cell_size=cell_size)
    inlet2_box = _opening_box(inlet2_wall, Lx, Ly, Lz, inlet2_center, inlet2_size, cell_size=cell_size) \
        if inlet2_wall is not None else None
    outlet2_box = _opening_box(outlet2_wall, Lx, Ly, Lz, outlet2_center, outlet2_size, cell_size=cell_size) \
        if outlet2_wall is not None else None

    paths = {}
    bm_path = f"{case_dir}/system/blockMeshDict"
    with open(bm_path, "w") as f:
        f.write(block_mesh_dict(Lx, Ly, Lz, cell_size))
    paths["blockMeshDict"] = bm_path

    ts_path = f"{case_dir}/system/topoSetDict"
    with open(ts_path, "w") as f:
        f.write(topo_set_dict(inlet_box, outlet_box, inlet2_box, outlet2_box))
    paths["topoSetDict"] = ts_path

    cp_path = f"{case_dir}/system/createPatchDict"
    with open(cp_path, "w") as f:
        f.write(create_patch_dict(has_inlet2=inlet2_box is not None, has_outlet2=outlet2_box is not None))
    paths["createPatchDict"] = cp_path

    return paths
