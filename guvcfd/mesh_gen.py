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


def _opening_box(wall, Lx, Ly, Lz, center_frac, size, eps=1e-4):
    """Return ((xmin,ymin,zmin),(xmax,ymax,zmax)) for a boxToFace opening on
    a given wall ("xMin" or "xMax"), centered at center_frac of (Ly, Lz)
    (fractions along y and z), with the given (width, height) opening size.
    """
    cy, cz = center_frac
    w, h = size
    y_lo, y_hi = cy * Ly - w / 2, cy * Ly + w / 2
    z_lo, z_hi = cz * Lz - h / 2, cz * Lz + h / 2
    if wall == "xMin":
        return (-eps, y_lo, z_lo), (eps, y_hi, z_hi)
    elif wall == "xMax":
        return (Lx - eps, y_lo, z_lo), (Lx + eps, y_hi, z_hi)
    raise ValueError(f"Unsupported wall {wall!r}, expected 'xMin' or 'xMax'")


def topo_set_dict(inlet_box, outlet_box):
    def fmt(box):
        (x0, y0, z0), (x1, y1, z1) = box
        return f"({x0:.6g} {y0:.6g} {z0:.6g}) ({x1:.6g} {y1:.6g} {z1:.6g})"

    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      topoSetDict;", "}", "",
        "actions", "(",
        "    {", "        name    inletFaces;", "        type    faceSet;",
        "        action  new;", "        source  boxToFace;",
        f"        box     {fmt(inlet_box)};", "    }",
        "    {", "        name    outletFaces;", "        type    faceSet;",
        "        action  new;", "        source  boxToFace;",
        f"        box     {fmt(outlet_box)};", "    }",
        ");", "",
    ]
    return "\n".join(lines)


def create_patch_dict():
    lines = [
        "FoamFile", "{", "    version     2.0;", "    format      ascii;",
        "    class       dictionary;", "    object      createPatchDict;", "}", "",
        "pointSync false;", "", "patches", "(",
        "    {", "        name        inlet;", "        patchInfo", "        {",
        "            type patch;", "        }",
        "        constructFrom set;", "        set         inletFaces;", "    }",
        "    {", "        name        outlet;", "        patchInfo", "        {",
        "            type patch;", "        }",
        "        constructFrom set;", "        set         outletFaces;", "    }",
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
                      outlet_wall="xMax", outlet_center=(0.5, 0.15), outlet_size=(0.3, 0.3)):
    """Write blockMeshDict, topoSetDict, createPatchDict into case_dir/system/.

    inlet/outlet center/size are fractions of (Ly, Lz) for center, and
    absolute meters for size, matching GenBlockmesh.py's convention (inlet
    high on one wall, outlet low on the opposite wall).
    """
    inlet_box = _opening_box(inlet_wall, Lx, Ly, Lz, inlet_center, inlet_size)
    outlet_box = _opening_box(outlet_wall, Lx, Ly, Lz, outlet_center, outlet_size)

    paths = {}
    bm_path = f"{case_dir}/system/blockMeshDict"
    with open(bm_path, "w") as f:
        f.write(block_mesh_dict(Lx, Ly, Lz, cell_size))
    paths["blockMeshDict"] = bm_path

    ts_path = f"{case_dir}/system/topoSetDict"
    with open(ts_path, "w") as f:
        f.write(topo_set_dict(inlet_box, outlet_box))
    paths["topoSetDict"] = ts_path

    cp_path = f"{case_dir}/system/createPatchDict"
    with open(cp_path, "w") as f:
        f.write(create_patch_dict())
    paths["createPatchDict"] = cp_path

    return paths
