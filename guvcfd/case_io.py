"""Read/write OpenFOAM ASCII field files for the fluence-mapping pipeline."""
import re
import shutil
from pathlib import Path

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


def clear_stale_run_output(case_dir):
    """Remove every trace of a previous run from case_dir before starting a
    fresh one: numbered time-step directories (all but "0"), postProcessing/,
    results.json, and solver logs.

    setup_case() and the solve pipelines only ever overwrite specific known
    files by name (0/ field files, system/ templates, etc. - see
    run_pipeline.setup_case's `mkdir(exist_ok=True)` + selective template
    copy) - they never clear the directory first. Without this, confirming
    "overwrite" on an already-populated case directory (app._confirm_overwrite_run)
    leaves stale artifacts from an earlier - possibly differently configured,
    or interrupted - run sitting alongside the new one instead of being
    replaced, e.g. old numbered snapshot directories the new run's own
    mid-pipeline cleanup only clears if it happens to run far enough to
    reach that step.
    """
    base = Path(case_dir)
    if not base.exists():
        return
    for child in base.iterdir():
        if child.is_dir() and child.name != "0" and re.fullmatch(r"\d+(\.\d+)?", child.name):
            shutil.rmtree(child, ignore_errors=True)
    postprocessing = base / "postProcessing"
    if postprocessing.exists():
        shutil.rmtree(postprocessing, ignore_errors=True)
    for name in ("results.json", "log.simpleFoam", "log.pimpleFoam", "log.blockMesh"):
        f = base / name
        if f.exists():
            f.unlink()


def read_boundary_patch_names(case_dir):
    """Read patch names from constant/polyMesh/boundary (canonical patch list)."""
    with open(f"{case_dir}/constant/polyMesh/boundary") as f:
        content = f.read()
    # Patch entries are top-level blocks: "    name\n    {\n ... type ...\n    }"
    return re.findall(r'^\s{4}(\w+)\s*\n\s{4}\{\s*\n\s*type', content, re.MULTILINE)


def _read_foam_count_and_list_body(content):
    """The `<N>\\n(\\n...\\n)` list block common to points/faces/etc. - each
    entry is on its own line with no bare ')' of its own (points are
    "(x y z)", faces are "4(i0 i1 i2 i3)"), so the first bare "\\n)" after
    the count really is the outer list's closing paren, not an entry's.
    """
    m = re.search(r'\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find a '<count>\\n(...)' list block")
    n = int(m.group(1))
    return n, m.group(2)


def _read_polymesh_points(case_dir):
    with open(f"{case_dir}/constant/polyMesh/points") as f:
        content = f.read()
    n, body = _read_foam_count_and_list_body(content)
    coords = re.findall(r'\(([^()]*)\)', body)
    points = [tuple(float(v) for v in c.split()) for c in coords]
    assert len(points) == n, f"points: parsed {len(points)} entries but header says {n}"
    return points


def _read_polymesh_faces(case_dir):
    with open(f"{case_dir}/constant/polyMesh/faces") as f:
        content = f.read()
    n, body = _read_foam_count_and_list_body(content)
    entries = re.findall(r'\d+\(([^()]*)\)', body)
    faces = [[int(i) for i in idxs.split()] for idxs in entries]
    assert len(faces) == n, f"faces: parsed {len(faces)} entries but header says {n}"
    return faces


def _read_polymesh_patch_range(case_dir, patch_name):
    with open(f"{case_dir}/constant/polyMesh/boundary") as f:
        content = f.read()
    m = re.search(rf'\n\s*{re.escape(patch_name)}\s*\n\s*\{{(.*?)\n\s*\}}', content, re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find patch '{patch_name}' in {case_dir}/constant/polyMesh/boundary")
    body = m.group(1)
    n_faces = int(re.search(r'nFaces\s+(\d+)\s*;', body).group(1))
    start_face = int(re.search(r'startFace\s+(\d+)\s*;', body).group(1))
    return start_face, n_faces


def read_patch_face_centers(case_dir, patch_name):
    """Face-center coordinates of a named boundary patch, read directly
    from constant/polyMesh/{points,faces,boundary} - self-contained (no
    WSL round-trip, no dependency on writeCellCentres having already run
    elsewhere in the pipeline). Face center = mean of its vertices, a fine
    approximation for the roughly-rectangular sub-faces this mesh produces.

    Returns an (N, 3) array in patch-face order - this order must exactly
    match a boundaryField's nonuniform List<vector> written back for this
    patch (see initial_fields.resolve_inlet_velocity) - a mismatch there
    is a hard OpenFOAM parse error, not a silent bug, hence the assert.
    """
    points = _read_polymesh_points(case_dir)
    faces = _read_polymesh_faces(case_dir)
    start_face, n_faces = _read_polymesh_patch_range(case_dir, patch_name)
    patch_faces = faces[start_face:start_face + n_faces]
    assert len(patch_faces) == n_faces, (
        f"patch '{patch_name}': expected {n_faces} faces at startFace {start_face}, got {len(patch_faces)}")
    centers = np.array([np.mean([points[i] for i in face], axis=0) for face in patch_faces])
    return centers


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
