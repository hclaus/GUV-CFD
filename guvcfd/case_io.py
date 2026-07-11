"""Read/write OpenFOAM ASCII field files for the fluence-mapping pipeline."""
import re
import numpy as np


def read_openfoam_scalar_field(path):
    """Read an OpenFOAM scalar field file (e.g. Cx, Cy, Cz), return a list of floats."""
    with open(path) as f:
        content = f.read()
    m = re.search(r'internalField\s+nonuniform\s+List<scalar>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find internalField nonuniform List<scalar> block in {path}")
    n = int(m.group(1))
    values = [float(v) for v in m.group(2).split('\n') if v.strip()]
    assert len(values) == n, f"{path}: parsed {len(values)} values but header says {n}"
    return values


def read_cell_centers(case_dir, time_dir="0"):
    """Read cell-center coordinates from <case_dir>/<time_dir>/{Cx,Cy,Cz}.

    Returns an (N, 3) array. These files are produced by running
    `postProcess -func writeCellCentres` in the case directory.
    """
    base = f"{case_dir}/{time_dir}"
    cx = read_openfoam_scalar_field(f"{base}/Cx")
    cy = read_openfoam_scalar_field(f"{base}/Cy")
    cz = read_openfoam_scalar_field(f"{base}/Cz")
    return np.column_stack([cx, cy, cz])


def read_boundary_patch_names(case_dir):
    """Read patch names from constant/polyMesh/boundary (canonical patch list)."""
    with open(f"{case_dir}/constant/polyMesh/boundary") as f:
        content = f.read()
    # Patch entries are top-level blocks: "    name\n    {\n ... type ...\n    }"
    return re.findall(r'^\s{4}(\w+)\s*\n\s{4}\{\s*\n\s*type', content, re.MULTILINE)


def write_scalar_field(case_dir, field_name, values, patch_names, time_dir="0", dimensions="[0 0 0 0 0 0 0]"):
    """Write a new OpenFOAM ASCII volScalarField, one value per cell.

    Boundary patches are written with `calculated`/`uniform 0` values since
    this field isn't intended to drive boundary conditions directly - it's
    meant for post-processing/visualization and as an fvOptions source input.
    """
    values = np.asarray(values, dtype=float)
    lines = [
        "FoamFile",
        "{",
        "    version     2.0;",
        "    format      ascii;",
        "    class       volScalarField;",
        f'    location    "{time_dir}";',
        f"    object      {field_name};",
        "}",
        "",
        f"dimensions      {dimensions};",
        "",
        "internalField   nonuniform List<scalar> ",
        str(len(values)),
        "(",
    ]
    lines.extend(f"{v:.6g}" for v in values)
    lines.append(")")
    lines.append(";")
    lines.append("")
    lines.append("boundaryField")
    lines.append("{")
    for patch in patch_names:
        lines.append(f"    {patch}")
        lines.append("    {")
        lines.append("        type            calculated;")
        lines.append("        value           uniform 0;")
        lines.append("    }")
    lines.append("}")
    lines.append("")

    out_path = f"{case_dir}/{time_dir}/{field_name}"
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    return out_path
