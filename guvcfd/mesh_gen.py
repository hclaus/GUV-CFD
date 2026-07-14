"""Generate a simple single-block room mesh directly from an Illuminate room's
dimensions, with inlet/outlet openings carved out of two opposite walls via
topoSet + createPatch (rather than GenBlockmesh.py's hand-built multi-block
approach, which encodes the opening position in the block topology itself).

Sequence: blockMesh -> topoSet -> createPatch -overwrite -> checkMesh.
"""
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


def _opening_box(wall, Lx, Ly, Lz, center_frac, size, eps=1e-4):
    """Return ((xmin,ymin,zmin),(xmax,ymax,zmax)) for a boxToFace opening on
    any of the 6 room walls (see _WALL_SPECS), centered at center_frac of
    the wall's two in-plane dimensions (fractions, in axis-index order -
    e.g. (y,z) for xMin/xMax, (x,y) for floor/ceiling), with the given
    (width, height) opening size.
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
    pos = normal_pos_fn(Lx, Ly, Lz)
    lo[normal_axis], hi[normal_axis] = pos - eps, pos + eps
    return tuple(lo), tuple(hi)


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
    inlet_box = _opening_box(inlet_wall, Lx, Ly, Lz, inlet_center, inlet_size)
    outlet_box = _opening_box(outlet_wall, Lx, Ly, Lz, outlet_center, outlet_size)
    inlet2_box = _opening_box(inlet2_wall, Lx, Ly, Lz, inlet2_center, inlet2_size) \
        if inlet2_wall is not None else None
    outlet2_box = _opening_box(outlet2_wall, Lx, Ly, Lz, outlet2_center, outlet2_size) \
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
