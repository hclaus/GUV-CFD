import pytest

from guvcfd.case_io import (
    clear_stale_run_output, read_patch_face_centers, read_patch_face_areas, read_openfoam_vector_field,
    latest_time_dir, read_latest_time_field, snapshot_openfoam_settings,
)

_POINTS = """FoamFile
{
    version     2.0;
    format      ascii;
    class       vectorField;
    object      points;
}

6
(
(0 0 0)
(1 0 0)
(2 0 0)
(0 1 0)
(1 1 0)
(2 1 0)
)
"""

_FACES = """FoamFile
{
    version     2.0;
    format      ascii;
    class       faceList;
    object      faces;
}

2
(
4(0 1 4 3)
4(1 2 5 4)
)
"""


def _boundary(n_faces=2, start_face=0):
    return f"""FoamFile
{{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}}

2
(
    inlet
    {{
        type            patch;
        nFaces          {n_faces};
        startFace       {start_face};
    }}
    walls
    {{
        type            wall;
        nFaces          0;
        startFace       {start_face + n_faces};
    }}
)
"""


def _write_polymesh(tmp_path, boundary_content=None):
    case_dir = tmp_path / "case"
    poly = case_dir / "constant" / "polyMesh"
    poly.mkdir(parents=True)
    (poly / "points").write_text(_POINTS)
    (poly / "faces").write_text(_FACES)
    (poly / "boundary").write_text(boundary_content or _boundary())
    return str(case_dir)


def test_reads_face_centers_in_patch_order(tmp_path):
    case_dir = _write_polymesh(tmp_path)
    centers = read_patch_face_centers(case_dir, "inlet")
    assert centers.shape == (2, 3)
    # face 0: points (0,0,0),(1,0,0),(1,1,0),(0,1,0) -> mean (0.5, 0.5, 0)
    assert centers[0] == pytest.approx((0.5, 0.5, 0.0))
    # face 1: points (1,0,0),(2,0,0),(2,1,0),(1,1,0) -> mean (1.5, 0.5, 0)
    assert centers[1] == pytest.approx((1.5, 0.5, 0.0))


def test_uses_start_face_offset(tmp_path):
    # A patch that doesn't start at face 0 - only the 2nd face belongs to it.
    boundary = f"""FoamFile
{{
    version     2.0;
    format      ascii;
    class       polyBoundaryMesh;
    object      boundary;
}}

1
(
    inlet
    {{
        type            patch;
        nFaces          1;
        startFace       1;
    }}
)
"""
    case_dir = _write_polymesh(tmp_path, boundary)
    centers = read_patch_face_centers(case_dir, "inlet")
    assert centers.shape == (1, 3)
    assert centers[0] == pytest.approx((1.5, 0.5, 0.0))


def test_raises_clear_error_for_unknown_patch(tmp_path):
    case_dir = _write_polymesh(tmp_path)
    with pytest.raises(RuntimeError, match="outlet"):
        read_patch_face_centers(case_dir, "outlet")


def test_face_count_mismatch_asserts(tmp_path):
    # boundary claims 5 faces starting at 0, but the mesh only has 2 -
    # must fail loudly (a hard OpenFOAM parse error downstream otherwise),
    # not silently write a truncated/wrong-length field.
    case_dir = _write_polymesh(tmp_path, _boundary(n_faces=5, start_face=0))
    with pytest.raises(AssertionError):
        read_patch_face_centers(case_dir, "inlet")


def test_reads_face_areas_of_unit_squares(tmp_path):
    # _POINTS/_FACES describes two adjacent 1x1 unit squares in the z=0
    # plane (see test_reads_face_centers_in_patch_order) - each face's
    # area should come out to exactly 1.0.
    case_dir = _write_polymesh(tmp_path)
    areas = read_patch_face_areas(case_dir, "inlet")
    assert areas.shape == (2,)
    assert areas == pytest.approx([1.0, 1.0])


def _write_vector_field_file(path, values):
    body = "\n".join(f"({v[0]} {v[1]} {v[2]})" for v in values)
    path.write_text(
        "FoamFile\n{\n    class volVectorField;\n    object U;\n}\n\n"
        f"internalField   nonuniform List<vector>\n{len(values)}\n(\n{body}\n)\n;\n"
    )


def test_reads_vector_field(tmp_path):
    path = tmp_path / "U"
    _write_vector_field_file(path, [(0.1, 0.2, 0.3), (-1.0, 0.0, 2.5)])
    values = read_openfoam_vector_field(str(path))
    assert values.shape == (2, 3)
    assert values[0] == pytest.approx((0.1, 0.2, 0.3))
    assert values[1] == pytest.approx((-1.0, 0.0, 2.5))


def test_vector_field_length_mismatch_asserts(tmp_path):
    path = tmp_path / "U"
    path.write_text(
        "FoamFile\n{\n    class volVectorField;\n    object U;\n}\n\n"
        "internalField   nonuniform List<vector>\n3\n(\n(0 0 0)\n(1 1 1)\n)\n;\n"
    )
    with pytest.raises(AssertionError):
        read_openfoam_vector_field(str(path))


def _make_stale_case(tmp_path):
    case_dir = tmp_path / "case"
    for name in ("0", "100", "500", "2000"):
        (case_dir / name).mkdir(parents=True)
    (case_dir / "0" / "U").write_text("initial field")
    (case_dir / "2000" / "U").write_text("final field")
    (case_dir / "postProcessing" / "volAverageLive1").mkdir(parents=True)
    (case_dir / "results.json").write_text("{}")
    (case_dir / "log.simpleFoam").write_text("log")
    (case_dir / "constant").mkdir()
    (case_dir / "system").mkdir()
    return case_dir


def test_clear_stale_run_output_removes_old_timesteps_and_artifacts(tmp_path):
    case_dir = _make_stale_case(tmp_path)
    clear_stale_run_output(str(case_dir))
    remaining = {p.name for p in case_dir.iterdir()}
    assert remaining == {"0", "constant", "system"}
    assert (case_dir / "0" / "U").exists()  # initial state untouched


def test_clear_stale_run_output_on_missing_dir_is_a_noop(tmp_path):
    clear_stale_run_output(str(tmp_path / "does-not-exist"))  # must not raise


def _write_scalar_field_file(path, values):
    body = "\n".join(str(v) for v in values)
    path.write_text(
        "FoamFile\n{\n    class volScalarField;\n    object T;\n}\n\n"
        f"internalField   nonuniform List<scalar>\n{len(values)}\n(\n{body}\n)\n;\n"
    )


def test_latest_time_dir_picks_highest_numbered_entry(tmp_path):
    for name in ("0", "100", "500", "2000"):
        (tmp_path / name).mkdir()
    (tmp_path / "constant").mkdir()  # non-numeric, must be ignored
    (tmp_path / "postProcessing").mkdir()
    assert latest_time_dir(str(tmp_path)) == "2000"


def test_latest_time_dir_handles_a_single_zero_directory(tmp_path):
    # Steady-state's own convention: each phase copies its final converged
    # state back into 0/ before cleanup, so 0 can legitimately be the ONLY
    # (and therefore correctly "latest") numbered directory present.
    (tmp_path / "0").mkdir()
    assert latest_time_dir(str(tmp_path)) == "0"


def test_latest_time_dir_raises_with_no_time_directories(tmp_path):
    (tmp_path / "constant").mkdir()
    with pytest.raises(RuntimeError):
        latest_time_dir(str(tmp_path))


def test_read_latest_time_field_reads_from_the_highest_numbered_directory(tmp_path):
    (tmp_path / "50").mkdir()
    (tmp_path / "100").mkdir()
    _write_scalar_field_file(tmp_path / "50" / "T", [1.0, 2.0])
    _write_scalar_field_file(tmp_path / "100" / "T", [3.0, 4.0, 5.0])
    assert read_latest_time_field(str(tmp_path), "T") == [3.0, 4.0, 5.0]


def test_snapshot_openfoam_settings_copies_present_files_into_dest_subdir(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "constant").mkdir()
    (tmp_path / "system" / "controlDict").write_text("maxCo 10;")
    (tmp_path / "system" / "fvSolution").write_text("solvers {}")
    # fvSchemes/turbulenceProperties/transportProperties deliberately absent -
    # a missing source file must be skipped, not raise.

    snapshot_openfoam_settings(str(tmp_path))

    dest = tmp_path / "system" / "project_schemes"
    assert (dest / "controlDict").read_text() == "maxCo 10;"
    assert (dest / "fvSolution").read_text() == "solvers {}"
    assert not (dest / "fvSchemes").exists()


def test_snapshot_openfoam_settings_overwrites_on_repeat_call(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "controlDict").write_text("maxCo 5;")

    snapshot_openfoam_settings(str(tmp_path))
    (tmp_path / "system" / "controlDict").write_text("maxCo 10;")
    snapshot_openfoam_settings(str(tmp_path))

    assert (tmp_path / "system" / "project_schemes" / "controlDict").read_text() == "maxCo 10;"
