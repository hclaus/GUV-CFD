"""Generate a simple single-block room mesh directly from an Illuminate room's
dimensions, with inlet/outlet openings carved out of two opposite walls via
topoSet + createPatch (rather than GenBlockmesh.py's hand-built multi-block
approach, which encodes the opening position in the block topology itself).

Sequence: blockMesh -> topoSet -> createPatch -overwrite -> checkMesh.
"""
import math

import numpy as np

from .wsl_utils import wsl_path as _wsl_path, write_wsl_text as _write_wsl_text

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


def _direction_grading(length, cell_size, wall_cell_size):
    """3-segment multi-grading spec for one block direction (both ends are
    walls for every direction in this single-block room mesh): one coarser
    cell of height wall_cell_size at each end, uniform cell_size cells in
    between. Used to deliberately raise near-wall y+ for a wall-function
    RAS mesh - see the "how do we know the correct value of nut" discussion
    this was built for: y+ = (first-cell height) * u_tau / nu, so a taller
    first cell raises y+ (the opposite direction from ordinary mesh
    refinement, which would only push y+ further into the buffer layer).

    Returns (total_n_cells, grading_str) - grading_str is the parenthesized
    "((lenFrac cellFrac ratio)(lenFrac cellFrac ratio)(lenFrac cellFrac
    ratio))" block for this one direction. ratio is 1 throughout (each
    segment's cells are uniform in size; only the two wall segments differ
    in size from the middle one) - deliberately not a smooth geometric
    transition, since this is a one-off sensitivity-test mesh, not a
    production near-wall layer.
    """
    interior_length = length - 2 * wall_cell_size
    if interior_length <= 0:
        raise ValueError(f"wall_cell_size ({wall_cell_size}) too large for direction length {length}")
    interior_n = max(1, round(interior_length / cell_size))
    total_n = interior_n + 2
    wall_frac_len = wall_cell_size / length
    interior_frac_len = 1 - 2 * wall_frac_len
    wall_frac_cells = 1 / total_n
    interior_frac_cells = interior_n / total_n
    grading = (
        f"(({wall_frac_len:.6g} {wall_frac_cells:.6g} 1)"
        f"({interior_frac_len:.6g} {interior_frac_cells:.6g} 1)"
        f"({wall_frac_len:.6g} {wall_frac_cells:.6g} 1))"
    )
    return total_n, grading


def _axis_cell_count(length, cell_size):
    """Number of cells blockMeshDict actually builds along one axis -
    round the cell COUNT, then blockMesh divides the room's exact
    dimension evenly into that many cells (see block_mesh_dict). This is
    the single source of truth both block_mesh_dict (mesh generation) and
    _opening_box (opening snapping) must agree on - see
    _actual_axis_cell_size's own docstring for what goes wrong when they
    don't.
    """
    return max(1, round(length / cell_size))


def _actual_axis_cell_size(length, cell_size):
    """The per-axis cell size blockMeshDict actually builds for `length`
    - differs from the nominal cell_size whenever length/cell_size isn't
    a whole number (e.g. a 5m room depth at a nominal 0.09m cell_size
    builds 56 cells of 0.089286m each, not 0.09m). _opening_box's
    snapping must snap to THIS, not the raw nominal cell_size, or the
    "snap to the mesh grid" step targets a coordinate that doesn't match
    any real cell face at all - silently reintroducing the exact
    boxToFace floating-point boundary-tie problem this snapping exists to
    avoid (confirmed directly: nominal-cell_size snapping at 0.09m on
    this room's 5m depth landed the carved inlet edge 18-28mm off every
    real grid line; at 0.08m, 18-37mm off - only cell sizes that divide
    every room dimension exactly, like 0.1m here, were actually safe).
    """
    return length / _axis_cell_count(length, cell_size)


def block_mesh_dict(Lx, Ly, Lz, cell_size=0.1, wall_cell_size=None):
    """Single-block box mesh covering the whole room, before opening carving.

    wall_cell_size: if given, grades the mesh so the single layer of cells
    against every wall (both ends of all 3 directions - every direction in
    this room mesh runs wall-to-wall) is this height instead of cell_size,
    with uniform cell_size cells in between (see _direction_grading). None
    (the default) keeps today's plain uniform simpleGrading (1 1 1).
    """
    if wall_cell_size is not None:
        nx, gx = _direction_grading(Lx, cell_size, wall_cell_size)
        ny, gy = _direction_grading(Ly, cell_size, wall_cell_size)
        nz, gz = _direction_grading(Lz, cell_size, wall_cell_size)
        grading = f"{gx} {gy} {gz}"
    else:
        nx = _axis_cell_count(Lx, cell_size)
        ny = _axis_cell_count(Ly, cell_size)
        nz = _axis_cell_count(Lz, cell_size)
        grading = "1 1 1"

    vertices = [(vx * Lx, vy * Ly, vz * Lz) for vx, vy, vz in _HEX_VERTICES]

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      blockMeshDict;", "}", "",
        "scale 1;", "", "vertices", "(",
    ]
    for v in vertices:
        lines.append(f"    ({v[0]:.6g} {v[1]:.6g} {v[2]:.6g})")
    lines += [");", "", "blocks", "(",
              f"    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading ({grading})",
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
        cell1, cell2 = _actual_axis_cell_size(dims[a1], cell_size), _actual_axis_cell_size(dims[a2], cell_size)
        lo[a1], hi[a1] = snap_outward(lo[a1], cell1, "lo"), snap_outward(hi[a1], cell1, "hi")
        lo[a2], hi[a2] = snap_outward(lo[a2], cell2, "lo"), snap_outward(hi[a2], cell2, "hi")
        if hi[a1] <= lo[a1]:
            hi[a1] = lo[a1] + cell1
        if hi[a2] <= lo[a2]:
            hi[a2] = lo[a2] + cell2
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


def suggest_opening_size_fix(wall, Lx, Ly, Lz, size, cell_size, fp_tol=1e-9):
    """(suggested_w, suggested_h): each axis of `size` rounded UP to the
    next exact multiple of the REAL per-axis cell size block_mesh_dict
    actually builds for that axis (see _actual_axis_cell_size - differs
    from the raw nominal cell_size whenever that room dimension isn't an
    exact multiple of it; using nominal directly here reproduced the same
    off-real-grid bug _opening_box had, fixed 2026-08-19) - unchanged
    (within fp_tol) if already exact. Pure size-only arithmetic,
    deliberately NOT coupled to position the way _opening_box's per-edge
    snap_outward is (that grows an off-grid opening by up to one cell
    PER EDGE; this grows it by up to one cell TOTAL) - see
    suggest_opening_center_fix for the separate, sequential position step
    meant to run after this one, using this function's own output as its
    `size` input.

    Mathematically identical to calling snap_outward(v, cell, "hi") on
    each axis value directly - ceil(v/cell)*cell is the same formula
    whether v is a position or a length, so this just reuses that
    already-tested primitive rather than duplicating its fp_tol handling.
    """
    _, _, (a1, a2) = _WALL_SPECS[wall]
    dims = (Lx, Ly, Lz)
    cell1, cell2 = _actual_axis_cell_size(dims[a1], cell_size), _actual_axis_cell_size(dims[a2], cell_size)
    return (snap_outward(size[0], cell1, "hi", fp_tol=fp_tol), snap_outward(size[1], cell2, "hi", fp_tol=fp_tol))


def _mod_phase(value, cell_size, fp_tol=1e-9):
    """value mod cell_size, snapped to exactly 0 or cell_size when within
    fp_tol of either boundary - guards the same binary floating-point
    noise snap_outward's own fp_tol guards against (e.g. 0.30000000000000004
    instead of exactly 0.3), applied to a modulo instead of a floor/ceil.
    """
    r = value % cell_size
    if r < fp_tol or (cell_size - r) < fp_tol:
        return 0.0
    return r


def _nearest_lattice_point(value, cell_size, phase, bias_point, fp_tol=1e-9):
    """Nearest point of the lattice {phase + k*cell_size : k in Z} to
    `value`. On an exact tie (distance cell_size/2 both ways, within
    fp_tol), picks whichever candidate is closer to bias_point instead of
    Python's round()-style banker's rounding - banker's rounding on a tie
    is exactly the kind of "resolves arbitrarily based on incidental
    parity" behavior snap_outward's own docstring already flags as a real
    bug class for opening EDGES (a 0.3m opening tie-breaking inward to
    0.2m); the same failure mode applies here to opening CENTERS, so it
    gets the same explicit, documented tie-break instead of an implicit
    language default.
    """
    n_lo = math.floor((value - phase) / cell_size + fp_tol)
    cand_lo = phase + n_lo * cell_size
    cand_hi = cand_lo + cell_size
    d_lo, d_hi = abs(value - cand_lo), abs(value - cand_hi)
    if abs(d_lo - d_hi) <= fp_tol:
        return cand_lo if abs(cand_lo - bias_point) <= abs(cand_hi - bias_point) else cand_hi
    return cand_lo if d_lo < d_hi else cand_hi


def suggest_opening_center_fix(wall, Lx, Ly, Lz, center_frac, size, cell_size, fp_tol=1e-9):
    """Given an opening's (width, height) already exact multiples of
    cell_size (call suggest_opening_size_fix first and pass ITS result as
    `size` here - see this function's own IMPORTANT note below for why
    that ordering matters), the nearest grid-aligned absolute center on
    each in-plane axis (a1, a2 per _WALL_SPECS), biased on exact ties
    toward that axis's own wall-midpoint (Lx/Ly/Lz /2 on that axis) so
    repeated snapping can't drift an opening toward a wall edge/corner.

    Returns ((current1_abs, current2_abs), (suggested1_abs, suggested2_abs))
    in ABSOLUTE meters (center_frac * that axis's room dimension), NOT
    center_frac - callers write this straight into settings' own
    {prefix}-y-input/{prefix}-z-input fields, which are already absolute
    meters, with no back-conversion needed.

    The math: edges land on grid lines exactly when
    (center - width/2) mod cell_size == 0 - i.e. valid centers form a
    lattice spaced exactly cell_size apart, offset by
    phase = (width/2) mod cell_size from the grid origin. For an EVEN
    cell count (e.g. 0.4m width / 0.1m cells = 4), phase is 0 and valid
    centers are exact grid lines. For an ODD cell count (e.g. 0.3m / 0.1m
    = 3), phase is cell_size/2 and valid centers sit at cell-CENTER
    offsets instead - a naive "round center to nearest cell_size
    multiple" check gets this case wrong. Either way the max needed shift
    is bounded at cell_size/2.

    IMPORTANT: only meaningful when `size`'s width/height are already
    exact cell_size multiples. If either axis's size is NOT an exact
    multiple, that axis's edges can never both land on the grid no matter
    what center is chosen (low_edge + width = high_edge, and width itself
    isn't a clean multiple) - callers must not present this function's
    output as a real fix for an axis whose size conflict was declined;
    see run_pipeline.walk_opening_alignment_conflicts's skip-center-on-
    declined-size rule.
    """
    _, _, (a1, a2) = _WALL_SPECS[wall]
    dims = (Lx, Ly, Lz)
    cell1, cell2 = _actual_axis_cell_size(dims[a1], cell_size), _actual_axis_cell_size(dims[a2], cell_size)
    cur1, cur2 = center_frac[0] * dims[a1], center_frac[1] * dims[a2]
    w, h = size
    phase1, phase2 = _mod_phase(w / 2, cell1, fp_tol), _mod_phase(h / 2, cell2, fp_tol)
    mid1, mid2 = dims[a1] / 2, dims[a2] / 2
    sug1 = _nearest_lattice_point(cur1, cell1, phase1, mid1, fp_tol)
    sug2 = _nearest_lattice_point(cur2, cell2, phase2, mid2, fp_tol)
    return (cur1, cur2), (sug1, sug2)


def opening_actual_area(wall, Lx, Ly, Lz, center_frac, size, cell_size):
    """The ACTUAL (grid-snapped) area of an opening, not its nominal
    (as-typed) area - see opening_grid_alignment. An inlet velocity
    computed from the nominal area (initial_fields.compute_inlet_velocity)
    delivers the wrong flow rate whenever the opening doesn't land exactly
    on the mesh grid: the boundary condition's velocity magnitude gets
    applied across whatever area blockMesh/topoSet actually carved, not the
    nominal one used to size it, over/under-delivering by their area ratio
    (confirmed directly: a 0.3x0.3m inlet outward-snapped to 0.4x0.4m on a
    0.1m mesh delivered 1.778x its intended flow rate - exactly the ratio
    check_ach_delivery flagged). Callers computing inlet velocity from ACH
    should use THIS area, not size[0]*size[1], so the requested ACH is
    delivered regardless of how the opening happens to snap to the grid.
    """
    _, actual = opening_grid_alignment(wall, Lx, Ly, Lz, center_frac, size, cell_size)
    return actual[0] * actual[1]


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


def _patch_entry(name, patch_type="patch"):
    return [
        "    {", f"        name        {name};", "        patchInfo", "        {",
        f"            type {patch_type};", "        }",
        "        constructFrom set;", f"        set         {name}Faces;", "    }",
    ]


def create_patch_dict(has_inlet2=False, has_outlet2=False, sealed=False):
    """sealed: True closes off inlet/outlet (and inlet2/outlet2, if present)
    as real `wall` patches instead of passive `patch` ones - a sealed
    (zero-ACH, fan-only-mixing) decay case has no ventilation at all, so
    these openings are physically walls, not just zero-velocity inlets/
    outlets (which is what previously produced a degenerate all-zero-flux
    system and crashed potentialFoam - see run_pipeline.setup_case's
    `sealed` docstring)."""
    patch_type = "wall" if sealed else "patch"
    patches = _patch_entry("inlet", patch_type) + _patch_entry("outlet", patch_type)
    if has_inlet2:
        patches += _patch_entry("inlet2", patch_type)
    if has_outlet2:
        patches += _patch_entry("outlet2", patch_type)

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
    case_dir_wsl = _wsl_path(case_dir)
    if case_dir_wsl != case_dir:
        _write_wsl_text(f"{case_dir_wsl}/system/mapFieldsDict", map_fields_dict(patch_names))
    else:
        with open(path, "w") as f:
            f.write(map_fields_dict(patch_names))
    return path


def decompose_par_dict(n_subdomains):
    """decomposeParDict for MPI-parallel decomposePar/mpirun/reconstructPar -
    scotch method needs no geometric coefficients (unlike simple/
    hierarchical, which need an explicit (nx ny nz) split), so this is the
    whole dict - good general-purpose default for an arbitrary room shape.
    """
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      decomposeParDict;", "}", "",
        f"numberOfSubdomains   {n_subdomains};", "",
        "method          scotch;", "",
    ]
    return "\n".join(lines)


def write_decompose_par_dict(case_dir, n_subdomains):
    case_dir_wsl = _wsl_path(case_dir)
    content = decompose_par_dict(n_subdomains)
    if case_dir_wsl != case_dir:
        _write_wsl_text(f"{case_dir_wsl}/system/decomposeParDict", content)
    else:
        with open(f"{case_dir}/system/decomposeParDict", "w") as f:
            f.write(content)
    return f"{case_dir}/system/decomposeParDict"


def write_mesh_dicts(case_dir, Lx, Ly, Lz, cell_size=0.1,
                      inlet_wall="xMin", inlet_center=(0.5, 0.85), inlet_size=(0.3, 0.3),
                      outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3),
                      inlet2_wall=None, inlet2_center=None, inlet2_size=None,
                      outlet2_wall=None, outlet2_center=None, outlet2_size=None,
                      sealed=False):
    """Write blockMeshDict, topoSetDict, createPatchDict into case_dir/system/.

    inlet/outlet center/size are fractions of the wall's two in-plane
    dimensions for center, and absolute meters for size (see
    _opening_box). inlet2_*/outlet2_*: an optional 2nd inlet/outlet, on any
    of the 6 walls independently of the primary one's wall - None (the
    default) means "no 2nd opening", carving the same 2-patch mesh as
    before this parameter existed.

    sealed: see create_patch_dict - carves the same opening geometry but
    closes it off as a wall patch instead of a flow patch.
    """
    inlet_box = _opening_box(inlet_wall, Lx, Ly, Lz, inlet_center, inlet_size, cell_size=cell_size)
    outlet_box = _opening_box(outlet_wall, Lx, Ly, Lz, outlet_center, outlet_size, cell_size=cell_size)
    inlet2_box = _opening_box(inlet2_wall, Lx, Ly, Lz, inlet2_center, inlet2_size, cell_size=cell_size) \
        if inlet2_wall is not None else None
    outlet2_box = _opening_box(outlet2_wall, Lx, Ly, Lz, outlet2_center, outlet2_size, cell_size=cell_size) \
        if outlet2_wall is not None else None

    # Written via a WSL-native process (not Windows-side open()) when
    # case_dir is an actual \\wsl.localhost\... path - these are the first
    # files written into a case directory that's brand new this session,
    # and blockMesh (also WSL-native) reads them right after. See
    # wsl_utils.write_wsl_text's docstring for why that cross-boundary
    # handoff isn't reliable for a directory neither side has "warmed up"
    # to yet. Falls back to a plain write for non-WSL paths (e.g. test
    # fixtures using local temp dirs), where no such handoff exists.
    case_dir_wsl = _wsl_path(case_dir)
    use_wsl_native = case_dir_wsl != case_dir

    def _write(relative_path, content):
        windows_path = f"{case_dir}/{relative_path}"
        if use_wsl_native:
            _write_wsl_text(f"{case_dir_wsl}/{relative_path}", content)
        else:
            with open(windows_path, "w") as f:
                f.write(content)
        return windows_path

    paths = {}
    paths["blockMeshDict"] = _write("system/blockMeshDict", block_mesh_dict(Lx, Ly, Lz, cell_size))
    paths["topoSetDict"] = _write("system/topoSetDict",
                                   topo_set_dict(inlet_box, outlet_box, inlet2_box, outlet2_box))
    paths["createPatchDict"] = _write(
        "system/createPatchDict",
        create_patch_dict(has_inlet2=inlet2_box is not None, has_outlet2=outlet2_box is not None,
                           sealed=sealed))

    return paths
