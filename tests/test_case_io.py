import pytest

from guvcfd.case_io import clear_stale_run_output, read_patch_face_centers

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
