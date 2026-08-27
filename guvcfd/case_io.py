"""Read/write OpenFOAM ASCII field files for the fluence-mapping pipeline."""
import re
import shutil
from pathlib import Path

import numpy as np

from .wsl_utils import (
    write_case_file as _write_case_file,
    wsl_path as _wsl_path,
    read_wsl_text as _read_wsl_text,
    run_wsl_or_raise as _run_wsl_or_raise,
)

# Every file that carries an OpenFOAM-level solver/physics constant not
# otherwise captured in any settings JSON (nOuterCorrectors, maxDeltaT,
# GAMG tolerances, discretization schemes, turbulence model choice,
# transport properties, ...) - see snapshot_openfoam_settings.
_SNAPSHOT_FILES = (
    "system/controlDict", "system/fvSolution", "system/fvSchemes",
    "constant/turbulenceProperties", "constant/transportProperties",
)


def snapshot_openfoam_settings(case_dir, dest_subdir="system/project_schemes", files=_SNAPSHOT_FILES):
    """Copy the actual solver dict files into <case_dir>/<dest_subdir>/ -
    a verbatim record of exactly what solver constants this run used,
    independent of whether a given value is exposed as a named setting
    anywhere. Motivated directly by 2026-08-07's live maxCo edits
    (5 -> 7 -> 10 mid-run), made by hand-editing controlDict outside the
    app entirely - no settings-JSON field, however complete, would have
    captured that on its own; a verbatim copy of the file that actually
    governed the solve does, unconditionally, and automatically covers
    every OTHER constant (nOuterCorrectors, maxDeltaT, GAMG tolerances,
    discretization schemes, turbulence model, ...) that was never worth
    promoting to its own named setting either.

    dest_subdir lives INSIDE system/ (not a sibling of it) deliberately -
    OpenFOAM only ever looks up specific named dictionaries by IOobject,
    never enumerates system/'s own contents, so an extra subdirectory
    there is completely inert to every OpenFOAM application reading this
    case.

    Safe to call more than once for the same case_dir (e.g. re-snapshotting
    after a mid-run hand-edit) - each call just overwrites the destination
    with the source files' current content, so the latest call always
    wins. Missing source files (e.g. a template variant without
    turbulenceProperties) are skipped, not fatal.

    Uses a WSL-native copy when case_dir is a real \\\\wsl.localhost\\...
    path (matching this module's own _read_text), rather than always
    shelling out to wsl.exe - a plain local case_dir (e.g. a test fixture
    under a pytest tmp_path) is copied with plain shutil instead, so tests
    never pay for a doomed-to-fail wsl.exe spawn.
    """
    case_dir_wsl = _wsl_path(case_dir)
    if case_dir_wsl != case_dir:
        copies = " ".join(f'[ -f "{f}" ] && cp "{f}" "{dest_subdir}/{Path(f).name}"; true' for f in files)
        cmd = f'mkdir -p "{dest_subdir}" && {copies}'
        _run_wsl_or_raise(cmd, case_dir_wsl, "snapshotting OpenFOAM settings")
    else:
        dest = Path(case_dir) / dest_subdir
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            src = Path(case_dir) / f
            if src.is_file():
                shutil.copy2(src, dest / src.name)


def _read_text(path):
    """Read `path`, via a WSL-native process when it's a real
    \\\\wsl.localhost\\... path rather than a plain Windows-side open() -
    these field files are freshly written by a WSL-native command
    (writeCellCentres, the solver itself) shortly before being read here,
    which hits the same cross-boundary visibility gap documented in
    wsl_utils.write_case_file. Falls back to a plain read for non-WSL
    paths (e.g. test fixtures).
    """
    path_for_wsl = _wsl_path(path)
    if path_for_wsl != path:
        return _read_wsl_text(path_for_wsl)
    with open(path) as f:
        return f.read()


def read_openfoam_scalar_field(path):
    """Read an OpenFOAM scalar field file (e.g. Cx, Cy, Cz), return a list of floats."""
    content = _read_text(path)
    m = re.search(r'internalField\s+nonuniform\s+List<scalar>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find internalField nonuniform List<scalar> block in {path}")
    n = int(m.group(1))
    values = [float(v) for v in m.group(2).split('\n') if v.strip()]
    assert len(values) == n, f"{path}: parsed {len(values)} values but header says {n}"
    return values


def read_openfoam_vector_field(path):
    """Read an OpenFOAM vector field file's internalField (e.g. U), return
    an (N, 3) array. See read_openfoam_scalar_field for the scalar analog.
    """
    content = _read_text(path)
    m = re.search(r'internalField\s+nonuniform\s+List<vector>\s*\n(\d+)\s*\n\(\n(.*?)\n\)', content, re.DOTALL)
    if not m:
        raise RuntimeError(f"Could not find internalField nonuniform List<vector> block in {path}")
    n = int(m.group(1))
    rows = re.findall(r'\(([^()]*)\)', m.group(2))
    values = np.array([[float(v) for v in row.split()] for row in rows], dtype=float)
    assert len(values) == n, f"{path}: parsed {len(values)} values but header says {n}"
    return values


_NUMERIC_DIR_RE = re.compile(r"^\d+(\.\d+)?$")


def latest_time_dir(case_dir):
    """The highest-numbered time directory directly under case_dir (as a
    plain string, matching OpenFOAM's own directory-naming convention -
    e.g. "500", "1500.5") - works whether the case keeps every write-
    interval snapshot at its own numbered time (decay mode, see
    _run_decay_pair) or has already copied its final converged state back
    into 0/ before cleanup (steady-state's Phase 1/2 chunks, see
    steady_state_pipeline._copy_latest_to_zero) - in the latter case 0
    is simply the only (and therefore highest) numbered entry present.

    Raises if case_dir has no numbered time directory at all (a case that
    was never actually run).
    """
    entries = [p.name for p in Path(case_dir).iterdir() if p.is_dir() and _NUMERIC_DIR_RE.match(p.name)]
    if not entries:
        raise RuntimeError(f"No time directories found in {case_dir}")
    return max(entries, key=float)


def read_latest_time_field(case_dir, field_name="T"):
    """Read `field_name`'s per-cell values from case_dir's latest time
    directory (see latest_time_dir) - the case's most advanced/final
    field state, for spatial (across-cells) analysis like coefficient-
    of-variation "how well mixed is this room right now," as opposed to
    decay_analysis.windowed_stats' TEMPORAL (across-iterations, room-
    average only) statistics.
    """
    return read_openfoam_scalar_field(f"{case_dir}/{latest_time_dir(case_dir)}/{field_name}")


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


def read_cell_volumes(case_dir, time_dir="0"):
    """Read per-cell volumes from <case_dir>/<time_dir>/V.

    Produced by running `postProcess -func writeCellVolumes` in the case
    directory (same pattern as read_cell_centers/writeCellCentres).
    Needed for any true room-average of a per-cell field (fluence rate,
    kUV, eACH) once the mesh isn't uniform - a locally-refined region
    packs many more (smaller) cells into the same physical space, so a
    plain, unweighted values.mean() silently overweights whatever got
    refined (confirmed as a real, ~4x error on a real locally-refined
    case: naive mean 11.6 uW/cm^2 vs. the true volume-weighted ~2.9).
    On a uniform mesh (every case before local refinement existed) this
    made no difference at all - volume-weighted and plain mean are
    identical when every cell has the same volume.
    """
    return np.array(read_openfoam_scalar_field(f"{case_dir}/{time_dir}/V"))


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
    content = _read_text(f"{case_dir}/constant/polyMesh/boundary")
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
    content = _read_text(f"{case_dir}/constant/polyMesh/points")
    n, body = _read_foam_count_and_list_body(content)
    coords = re.findall(r'\(([^()]*)\)', body)
    points = [tuple(float(v) for v in c.split()) for c in coords]
    assert len(points) == n, f"points: parsed {len(points)} entries but header says {n}"
    return points


def _read_polymesh_faces(case_dir):
    content = _read_text(f"{case_dir}/constant/polyMesh/faces")
    n, body = _read_foam_count_and_list_body(content)
    entries = re.findall(r'\d+\(([^()]*)\)', body)
    faces = [[int(i) for i in idxs.split()] for idxs in entries]
    assert len(faces) == n, f"faces: parsed {len(faces)} entries but header says {n}"
    return faces


def _read_polymesh_patch_range(case_dir, patch_name):
    content = _read_text(f"{case_dir}/constant/polyMesh/boundary")
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


def read_patch_face_areas(case_dir, patch_name):
    """Face areas [m^2] of a named boundary patch, in the same patch-face
    order as read_patch_face_centers - needed to flux-weight particle
    seeding (see lagrangian_tracking.seed_inlet_particles): a face's
    contribution to volumetric flow is area * normal velocity, not just
    velocity alone, so faces must be weighted by their own area too.

    Computed by triangulating each face as a fan from its own centroid
    (works for the roughly-planar quads/n-gons this mesh produces,
    convex or not) and summing triangle areas via the cross-product
    magnitude - the standard robust polygon-area-in-3D method.
    """
    points = _read_polymesh_points(case_dir)
    faces = _read_polymesh_faces(case_dir)
    start_face, n_faces = _read_polymesh_patch_range(case_dir, patch_name)
    patch_faces = faces[start_face:start_face + n_faces]
    areas = np.empty(n_faces)
    for i, face in enumerate(patch_faces):
        verts = np.array([points[idx] for idx in face])
        centroid = verts.mean(axis=0)
        tri_areas = 0.5 * np.linalg.norm(
            np.cross(verts - centroid, np.roll(verts, -1, axis=0) - centroid), axis=1)
        areas[i] = tri_areas.sum()
    return areas


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
    _write_case_file(case_dir, f"{time_dir}/{field_name}", "\n".join(lines))
    return out_path
